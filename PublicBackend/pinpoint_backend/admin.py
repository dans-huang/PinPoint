from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

from .config import (
    ConfigurationError,
    Settings,
    authentication_mode_from_environment,
    public_link_configuration_from_environment,
)
from .plaud import PlaudPartnerClient, PlaudServiceError
from .public_links import custom_scheme_activation_url, public_activation_url
from .security import local_activation_verifier, managed_invite_verifier
from .state import (
    ActiveDeviceBindings,
    BetaInvitation,
    BetaInvitationUnavailable,
    DeviceBindingUnavailable,
    LocalActivation,
    LocalActivationUnavailable,
    RecorderLedgerReconciliationRequired,
    StateStore,
    StateStoreError,
    TesterMembershipUnavailable,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Operate PinPoint hosted membership and self-hosted activation"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List member ids and active/disabled state")
    invitations = commands.add_parser("invite", help="Create, list, or revoke invitations")
    invitation_commands = invitations.add_subparsers(dest="invite_command", required=True)
    create_invitation = invitation_commands.add_parser(
        "create",
        help="Create one expiring invitation and print its code once",
    )
    create_invitation.add_argument("--label", required=True, help="Operator-only member label")
    create_invitation.add_argument(
        "--expires-in-hours",
        type=_invite_expiry_hours,
        default=7 * 24,
        help="Invitation lifetime from 1 to 2160 hours (default: 168)",
    )
    invitation_commands.add_parser("list", help="List invitation metadata without secrets")
    revoke_invitation = invitation_commands.add_parser(
        "revoke",
        help="Revoke one unused invitation",
    )
    revoke_invitation.add_argument("invite_id", help="Opaque invitation id")
    activations = commands.add_parser(
        "activation",
        help="Create a one-time self-hosted activation code",
    )
    activation_commands = activations.add_subparsers(
        dest="activation_command",
        required=True,
    )
    activation_commands.add_parser(
        "create",
        help="Print one activation code that expires in 10 minutes",
    )
    bindings = commands.add_parser("bindings", help="List one member's recorder ledger")
    bindings.add_argument("user_id", help="Stable PinPoint user id")
    release = commands.add_parser(
        "release",
        help="Release one exact ledger-owned recorder through Plaud",
    )
    release.add_argument("user_id", help="Stable PinPoint user id")
    release.add_argument("serial_number", help="Exact recorder serial from the ledger")
    for command in ("disable", "enable"):
        action = commands.add_parser(command, help=f"{command.title()} one invited member")
        action.add_argument("user_id", help="Stable PinPoint user id")
    arguments = parser.parse_args(argv)

    database_path = os.environ.get("PINPOINT_STATE_DB_PATH", "").strip()
    if not database_path:
        parser.error("PINPOINT_STATE_DB_PATH is required")
    if not Path(database_path).expanduser().is_file():
        print("PinPoint membership ledger was not found", file=sys.stderr)
        return 1
    try:
        state = StateStore(database_path)
        if arguments.command == "invite":
            if authentication_mode_from_environment() != "hosted":
                raise ConfigurationError(
                    "Invitations are only available in hosted mode"
                )
            if arguments.invite_command == "create":
                # Validate the delivery URL before consuming the one chance to
                # print a new invitation secret.
                invite_base_url, _ = public_link_configuration_from_environment()
                invitation, raw_code = _create_invitation(
                    state,
                    label=arguments.label,
                    expires_in_hours=arguments.expires_in_hours,
                )
                record = _invitation_record(invitation)
                record["invite_code"] = raw_code
                custom_url = custom_scheme_activation_url(raw_code)
                record["activation_url"] = (
                    public_activation_url(invite_base_url, raw_code)
                    if invite_base_url is not None
                    else custom_url
                )
                record["custom_scheme_activation_url"] = custom_url
                print(json.dumps(record, separators=(",", ":")))
                return 0
            if arguments.invite_command == "list":
                for invitation in state.list_beta_invites():
                    print(json.dumps(_invitation_record(invitation), separators=(",", ":")))
                return 0
            invitation = state.revoke_beta_invite(arguments.invite_id)
            print(json.dumps(_invitation_record(invitation), separators=(",", ":")))
            return 0

        if arguments.command == "activation":
            if authentication_mode_from_environment() != "self_hosted":
                raise ConfigurationError(
                    "Local activation is only available in self_hosted mode"
                )
            activation, raw_code = _create_local_activation(state)
            record = _local_activation_record(activation)
            record["activation_code"] = raw_code
            print(json.dumps(record, separators=(",", ":")))
            return 0

        if arguments.command == "list":
            for tester in state.list_beta_users():
                print(json.dumps({
                    "user_id": tester.user_id,
                    "state": tester.state,
                    "authorized_at": tester.authorized_at,
                    "updated_at": tester.updated_at,
                }, separators=(",", ":")))
            return 0

        if arguments.command == "bindings":
            for binding in state.list_device_bindings(arguments.user_id):
                print(json.dumps({
                    "user_id": binding.user_id,
                    "serial_number": binding.serial_number,
                    "device_type": binding.device_type,
                    "state": binding.state,
                    "updated_at": binding.updated_at,
                }, separators=(",", ":")))
            return 0

        if arguments.command == "release":
            plaud = _operator_plaud_client(state)
            with state.device_lifecycle_lock(
                user_id=arguments.user_id,
                serial_number=arguments.serial_number,
            ):
                device_type = state.begin_operator_device_release(
                    user_id=arguments.user_id,
                    serial_number=arguments.serial_number,
                )
                try:
                    plaud.unbind_device(
                        user_id=arguments.user_id,
                        serial_number=arguments.serial_number,
                        device_type=device_type,
                    )
                except PlaudServiceError:
                    raise DeviceBindingUnavailable(
                        "Plaud did not confirm release; ledger remains release_pending"
                    )
                state.mark_device_released(
                    user_id=arguments.user_id,
                    serial_number=arguments.serial_number,
                )
            print(f"{arguments.user_id}\t{arguments.serial_number}\treleased")
            return 0

        enabled = arguments.command == "enable"
        state.set_beta_user_enabled(arguments.user_id, enabled=enabled)
        print(f"{arguments.user_id}\t{'active' if enabled else 'disabled'}")
        return 0
    except TesterMembershipUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except BetaInvitationUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except LocalActivationUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ActiveDeviceBindings as exc:
        serials = ", ".join(binding.serial_number for binding in exc.bindings)
        print(
            "Refusing to disable: release active recorder binding(s) first: " + serials,
            file=sys.stderr,
        )
        print(
            f"Run `bindings {arguments.user_id}` then `release {arguments.user_id} SERIAL`.",
            file=sys.stderr,
        )
        return 1
    except DeviceBindingUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RecorderLedgerReconciliationRequired as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ConfigurationError, StateStoreError, ValueError):
        print("PinPoint operator configuration or ledger is unavailable", file=sys.stderr)
        return 1


def _invite_expiry_hours(value: str) -> int:
    try:
        hours = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a whole number of hours") from exc
    if not 1 <= hours <= 90 * 24:
        raise argparse.ArgumentTypeError("must be between 1 and 2160 hours")
    return hours


def _create_invitation(
    state: StateStore,
    *,
    label: str,
    expires_in_hours: int,
) -> tuple[BetaInvitation, str]:
    identity_secret = os.environ.get("PINPOINT_USER_ID_SECRET_V1", "")
    if len(identity_secret.encode("utf-8")) < 32:
        raise ConfigurationError(
            "PINPOINT_USER_ID_SECRET_V1 must be loaded to create an invitation"
        )
    expires_at = time.time() + expires_in_hours * 60 * 60
    for _ in range(5):
        raw_code = "ppi_" + secrets.token_urlsafe(32)
        invite_id = "inv_" + secrets.token_urlsafe(12)
        try:
            invitation = state.create_beta_invite(
                invite_id=invite_id,
                invite_verifier=managed_invite_verifier(identity_secret, raw_code),
                label=label,
                expires_at=expires_at,
            )
        except BetaInvitationUnavailable:
            continue
        return invitation, raw_code
    raise StateStoreError("Could not allocate a unique invitation")


def _invitation_record(invitation: BetaInvitation) -> dict[str, object]:
    return {
        "invite_id": invitation.invite_id,
        "label": invitation.label,
        "state": invitation.state,
        "created_at": invitation.created_at,
        "expires_at": invitation.expires_at,
        "consumed_at": invitation.consumed_at,
        "consumed_by_user_id": invitation.consumed_by_user_id,
        "revoked_at": invitation.revoked_at,
    }


def _create_local_activation(
    state: StateStore,
) -> tuple[LocalActivation, str]:
    identity_secret = os.environ.get("PINPOINT_USER_ID_SECRET_V1", "")
    if len(identity_secret.encode("utf-8")) < 32:
        raise ConfigurationError(
            "PINPOINT_USER_ID_SECRET_V1 must be loaded to create an activation"
        )
    now = time.time()
    for _ in range(5):
        raw_code = "ppl_" + secrets.token_urlsafe(32)
        activation_id = "act_" + secrets.token_urlsafe(12)
        try:
            activation = state.create_local_activation(
                activation_id=activation_id,
                activation_verifier=local_activation_verifier(
                    identity_secret,
                    raw_code,
                ),
                expires_at=now + 10 * 60,
            )
        except LocalActivationUnavailable:
            continue
        return activation, raw_code
    raise StateStoreError("Could not allocate a unique local activation")


def _local_activation_record(activation: LocalActivation) -> dict[str, object]:
    return {
        "activation_id": activation.activation_id,
        "state": activation.state,
        "created_at": activation.created_at,
        "expires_at": activation.expires_at,
        "expires_in_seconds": 10 * 60,
    }


def _operator_plaud_client(state: StateStore) -> PlaudPartnerClient:
    settings = Settings.from_environment()
    if Path(settings.state_db_path).expanduser().resolve() != state.path:
        raise ConfigurationError("Operator and service state database paths differ")
    return PlaudPartnerClient(
        client_id=settings.plaud_client_id,
        client_secret=settings.plaud_client_secret,
        api_key=settings.plaud_api_key,
        domain=settings.plaud_api_domain,
        user_token_ttl_seconds=settings.plaud_user_token_ttl_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
