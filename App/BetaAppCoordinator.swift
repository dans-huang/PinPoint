import UIKit

final class BetaAppCoordinator {
    private let window: UIWindow
    private let navigationController = UINavigationController()
    private let sessionStore = BetaSessionStore()
    private lazy var deviceService = BetaDeviceService()
    private var configuration: BetaConfiguration?
    private var apiClient: PinpointAPIClient?
    private var currentSession: BetaSession?
    private var refreshTimer: Timer?
    private var refreshInFlight = false
    private let accessValidationInterval: TimeInterval = 5 * 60
    private var accessValidationTimer: Timer?
    private var accessValidationInFlight = false
    private var lastAccessValidationAt: Date?
    private var accessResumePending = false
    private var accessFailureAlert: UIAlertController?
    private var pendingExpiryMessage: String?
    private var signOutInFlight = false
    private var signOutAwaitingResolution = false
    private var bootstrapPending = false
    private var resumeClearsPendingSignOut = false
    private var authOperationGeneration = 0
    private let defaults = UserDefaults.standard
    private var pendingInvitationCode: String?
    private var pendingInvitationError: String?
    private var pendingLocalActivationCode: String?
    private var pendingLocalActivationError: String?
    private var intelligenceCoordinator: BetaIntelligenceCoordinator?

    private enum Keys {
        static let pendingSignOut = "PinPoint.pendingSignOut"
    }

    init(window: UIWindow) {
        self.window = window
        navigationController.setNavigationBarHidden(true, animated: false)
    }

    func start(invitationURL: URL? = nil) {
        window.rootViewController = navigationController
        window.makeKeyAndVisible()
        deviceService.onSessionExpired = { [weak self] in self?.refreshAfterServiceExpiry() }
        deviceService.onAccessLeaseExpired = { [weak self] in
            self?.validateAccessAfterLeaseExpiry()
        }

        #if DEBUG
        if let preview = BetaPreviewFactory.viewController(arguments: ProcessInfo.processInfo.arguments) {
            navigationController.setViewControllers([preview], animated: false)
            return
        }
        #endif

        switch BetaConfiguration.load() {
        case .success(let configuration):
            self.configuration = configuration
            apiClient = PinpointAPIClient(baseURL: configuration.apiBaseURL)
        case .failure(let error):
            showWelcome(blockingError: error.localizedDescription)
            return
        }

        if let invitationURL {
            receiveActivationURL(invitationURL)
        }

        do {
            guard let session = try sessionStore.load() else {
                if hasPendingSignOut {
                    guard clearPendingSignOut() else {
                        showPendingSignOutStorageFailure()
                        return
                    }
                }
                showWelcome()
                return
            }
            if hasPendingSignOut && !isTrusted(session) {
                showSessionStorageFailure(
                    detail: "PinPoint cannot safely finish the previous sign-out because the saved session no longer matches this service. Automatic sync remains stopped."
                )
                return
            }
            guard isTrusted(session) else {
                do {
                    try sessionStore.clear()
                    guard try sessionStore.load() == nil else {
                        throw SessionStoreError.invalidSession
                    }
                } catch {
                    showSessionStorageFailure(
                        detail: "PinPoint could not safely remove an untrusted saved session. Automatic sync remains stopped."
                    )
                    return
                }
                showWelcome(initialError: "The saved session did not match this PinPoint service. Sign in again to continue.")
                return
            }
            currentSession = session
            // A launch URL is an enrollment credential, not something an
            // existing account should carry forward to a later sign-in.
            pendingInvitationCode = nil
            pendingInvitationError = nil
            pendingLocalActivationCode = nil
            pendingLocalActivationError = nil
            if hasPendingSignOut {
                bootstrapPending = true
                signOutAwaitingResolution = true
                navigationController.setViewControllers([
                    BetaRestartViewController(
                        titleText: "Finish your previous sign-out",
                        detailText: "PinPoint is still disconnected. Choose whether to retry sign-out or explicitly resume this account.",
                        symbolName: "pause.circle.fill",
                        noteText: "No recorder or cloud work will start until you decide."
                    )
                ], animated: false)
                DispatchQueue.main.async { [weak self] in
                    self?.presentUnconfirmedSignOut(error: PendingSignOutError.awaitingResolution)
                }
                return
            }
            bootstrapPending = true
            // Do not initialize the Plaud SDK from a cached bearer token until
            // the backend confirms this tester and session are still active.
            navigationController.setViewControllers([
                BetaRestartViewController(
                    titleText: "Checking your PinPoint sign-in…",
                    detailText: "Confirming access before the recorder reconnects.",
                    symbolName: "lock.shield.fill",
                    noteText: nil
                )
            ], animated: false)
            refreshSession(session, showFailure: true)
        } catch SessionStoreError.invalidSession where !hasPendingSignOut {
            // BetaSessionStore has already proved the damaged Keychain item
            // was deleted, so a fresh sign-in is now safe.
            showWelcome(initialError: "The damaged saved sign-in was removed. Sign in again to continue.")
        } catch {
            showSessionStorageFailure(
                detail: hasPendingSignOut
                    ? "PinPoint could not safely read the session needed to finish your previous sign-out. Automatic sync remains stopped."
                    : "PinPoint could not securely read or clear the saved sign-in. Automatic sync remains stopped."
            )
        }
    }

    func sceneDidBecomeActive() {
        if let pendingExpiryMessage {
            expireCurrentSession(message: pendingExpiryMessage)
            return
        }
        guard let session = currentSession,
              !signOutInFlight,
              !signOutAwaitingResolution else { return }
        if lastAccessValidationAt.map({ Date().timeIntervalSince($0) >= accessValidationInterval }) ?? true {
            // Suspend before checking in-flight request state. If a token
            // refresh is already running, its callback becomes the access
            // decision and must explicitly resume or remain fail-closed.
            accessResumePending = true
            refreshTimer?.invalidate()
            refreshTimer = nil
            deviceService.suspendForAccessValidation(
                message: "Confirming that this PinPoint sign-in is still active before reconnecting."
            )
            validateAccess(
                session,
                suspendBeforeRequest: false,
                pauseMessage: "Confirming that this PinPoint sign-in is still active before reconnecting."
            )
            return
        }
        scheduleAccessValidation()
        let nextExpiry = min(session.sessionExpiresAt, session.plaudTokenExpiresAt)
        if nextExpiry.timeIntervalSinceNow <= 5 * 60 {
            refreshSession(session, showFailure: false)
        } else {
            scheduleSessionRefresh(for: session)
        }
    }

    func handleInvitationURL(_ url: URL) {
        guard currentSession == nil, !hasPendingSignOut else { return }
        receiveActivationURL(url)
        guard let welcome = navigationController.topViewController as? BetaWelcomeViewController else {
            return
        }
        welcome.loadViewIfNeeded()
        switch configuration?.deploymentMode {
        case .hosted:
            if let pendingInvitationCode {
                welcome.applyInvitationCode(pendingInvitationCode)
            } else if let pendingInvitationError {
                welcome.showInvitationLinkError(pendingInvitationError)
            }
        case .selfHosted:
            if let pendingLocalActivationCode {
                welcome.applyLocalActivationCode(pendingLocalActivationCode)
            } else if let pendingLocalActivationError {
                welcome.showLocalActivationLinkError(pendingLocalActivationError)
            }
        case nil:
            break
        }
    }

    private func receiveActivationURL(_ url: URL) {
        switch configuration?.deploymentMode {
        case .hosted:
            guard let code = BetaInvitationLink.code(from: url) else {
                pendingInvitationCode = nil
                pendingInvitationError = "This invitation link is not valid. Ask the sender for a new invitation or enter the code manually."
                return
            }
            pendingInvitationCode = code
            pendingInvitationError = nil
        case .selfHosted:
            guard let code = PinpointLocalActivationLink.code(from: url) else {
                pendingLocalActivationCode = nil
                pendingLocalActivationError = "This local activation link is not valid. Create a new activation code from your PinPoint service."
                return
            }
            pendingLocalActivationCode = code
            pendingLocalActivationError = nil
        case nil:
            break
        }
    }

    private func showWelcome(blockingError: String? = nil, initialError: String? = nil) {
        bootstrapPending = false
        let effectiveBlockingError = blockingError ?? configurationFailureMessage()
        let deploymentMode = configuration?.deploymentMode ?? .hosted
        let activationError: String?
        switch deploymentMode {
        case .hosted:
            activationError = effectiveBlockingError == nil ? pendingInvitationError : nil
        case .selfHosted:
            activationError = effectiveBlockingError == nil ? pendingLocalActivationError : nil
        }
        let welcome = BetaWelcomeViewController(
            deploymentMode: deploymentMode,
            blockingConfigurationError: effectiveBlockingError,
            initialError: initialError,
            activationCode: deploymentMode == .hosted
                ? pendingInvitationCode
                : pendingLocalActivationCode
        )
        welcome.onRequestNonce = { [weak self] completion in
            guard let apiClient = self?.apiClient else {
                completion(.failure(ConfigurationError.missingAPIBaseURL))
                return
            }
            apiClient.requestSignInNonce(completion: completion)
        }
        welcome.onSignIn = { [weak self, weak welcome] payload, completion in
            guard let self, let apiClient = self.apiClient else {
                completion(.failure(ConfigurationError.missingAPIBaseURL))
                return
            }
            guard !self.hasPendingSignOut else {
                completion(.failure(PendingSignOutError.awaitingResolution))
                return
            }
            apiClient.signInWithApple(
                identityToken: payload.identityToken,
                authorizationCode: payload.authorizationCode,
                nonce: payload.nonce,
                inviteCode: payload.inviteCode
            ) { result in
                DispatchQueue.main.async {
                    switch result {
                    case .success(let session):
                        guard !self.hasPendingSignOut else {
                            completion(.failure(PendingSignOutError.awaitingResolution))
                            return
                        }
                        guard self.isTrusted(session) else {
                            completion(.failure(ConfigurationError.invalidPlaudDomain))
                            return
                        }
                        do {
                            let persisted = try self.persistSession(session)
                            self.currentSession = persisted
                            self.pendingInvitationCode = nil
                            self.pendingInvitationError = nil
                            self.pendingLocalActivationCode = nil
                            self.pendingLocalActivationError = nil
                            self.lastAccessValidationAt = Date()
                            self.scheduleSessionRefresh(for: persisted)
                            self.scheduleAccessValidation()
                            completion(.success(()))
                            self.showDeviceSetup(session: persisted)
                        } catch {
                            completion(.failure(error))
                            _ = welcome
                        }
                    case .failure(let error):
                        completion(.failure(error))
                    }
                }
            }
        }
        welcome.onCreateLocalSession = { [weak self, weak welcome] activationCode, completion in
            guard let self, let apiClient = self.apiClient else {
                completion(.failure(ConfigurationError.missingAPIBaseURL))
                return
            }
            guard self.configuration?.deploymentMode == .selfHosted else {
                completion(.failure(ConfigurationError.invalidDeploymentMode))
                return
            }
            guard !self.hasPendingSignOut else {
                completion(.failure(PendingSignOutError.awaitingResolution))
                return
            }
            apiClient.createLocalSession(activationCode: activationCode) { result in
                DispatchQueue.main.async {
                    switch result {
                    case .success(let session):
                        guard !self.hasPendingSignOut else {
                            completion(.failure(PendingSignOutError.awaitingResolution))
                            return
                        }
                        guard self.isTrusted(session) else {
                            completion(.failure(ConfigurationError.invalidDeploymentMode))
                            return
                        }
                        do {
                            let persisted = try self.persistSession(session)
                            self.currentSession = persisted
                            self.pendingLocalActivationCode = nil
                            self.pendingLocalActivationError = nil
                            self.lastAccessValidationAt = Date()
                            self.scheduleSessionRefresh(for: persisted)
                            self.scheduleAccessValidation()
                            completion(.success(()))
                            self.showDeviceSetup(session: persisted)
                        } catch {
                            completion(.failure(error))
                            _ = welcome
                        }
                    case .failure(let error):
                        completion(.failure(error))
                    }
                }
            }
        }
        navigationController.setViewControllers([welcome], animated: false)
        if let activationError {
            welcome.loadViewIfNeeded()
            switch deploymentMode {
            case .hosted:
                welcome.showInvitationLinkError(activationError)
            case .selfHosted:
                welcome.showLocalActivationLinkError(activationError)
            }
        }
    }

    private func configurationFailureMessage() -> String? {
        configuration == nil ? ConfigurationError.missingAPIBaseURL.localizedDescription : nil
    }

    private func showDeviceSetup(session: BetaSession) {
        guard let apiClient else {
            showWelcome(blockingError: ConfigurationError.missingAPIBaseURL.localizedDescription)
            return
        }
        let setup = BetaDeviceSetupViewController()
        deviceService.onStateChange = { [weak self, weak setup] state in
            guard let self else { return }
            if case .ready(let device) = state,
               let setup,
               self.navigationController.topViewController === setup {
                self.showHome(device: device)
                return
            }
            setup?.showsReleaseAction = self.deviceService.hasSavedRecorderAssociation
            setup?.showsLocalDataAction = !self.deviceService.recordings.isEmpty
            setup?.render(state)
        }
        setup.onScan = { [weak self] in self?.deviceService.startScan() }
        setup.onConnect = { [weak self] device in self?.deviceService.connect(device) }
        setup.onRetryOwnership = { [weak self] in self?.deviceService.retryOwnershipCheck() }
        setup.onUseAnotherRecorder = { [weak self] in self?.deviceService.forgetCurrentAttempt() }
        setup.onContinue = { [weak self] device in self?.showHome(device: device) }
        setup.onRemoveRecorder = { [weak self, weak setup] in
            self?.deviceService.removeRecorder { result in
                switch result {
                case .success:
                    guard let self, let session = self.currentSession else { return }
                    self.showDeviceSetup(session: session)
                case .failure(let error):
                    setup?.showRecorderRemovalError(error.localizedDescription)
                }
            }
        }
        setup.onDeleteLocalData = { [weak self] in self?.confirmLocalDataDeletion() }
        setup.onSignOut = { [weak self] in self?.signOut() }
        navigationController.setViewControllers([setup], animated: true)
        guard deviceService.configure(session: session, pinpointAPI: apiClient) else {
            setup.showsReleaseAction = deviceService.hasSavedRecorderAssociation
            setup.showsLocalDataAction = !deviceService.recordings.isEmpty
            setup.render(deviceService.state)
            return
        }
        setup.showsReleaseAction = deviceService.hasSavedRecorderAssociation
        setup.showsLocalDataAction = !deviceService.recordings.isEmpty
        if !deviceService.reconnectSavedDeviceIfAvailable() {
            deviceService.beginOnboarding()
        }
    }

    private func showHome(device: ConnectedPlaudDevice) {
        guard let session = currentSession, let configuration else {
            showWelcome(blockingError: ConfigurationError.missingAPIBaseURL.localizedDescription)
            return
        }
        let intelligence: BetaIntelligenceCoordinator
        if let existing = intelligenceCoordinator, existing.session.userID == session.userID {
            existing.updateSession(session)
            intelligence = existing
        } else {
            intelligence = BetaIntelligenceCoordinator(
                client: BetaIntelligenceAPIClient(baseURL: configuration.apiBaseURL),
                session: session
            )
            intelligenceCoordinator = intelligence
        }
        intelligence.markedMomentsProvider = { [weak self] recording in
            self?.deviceService.markedMoments(for: recording)
        }
        let home = BetaHomeViewController(device: device, recordings: deviceService.recordings)
        deviceService.onStateChange = { [weak home] state in home?.render(connectionState: state) }
        deviceService.onRecordingsChange = { [weak home, weak intelligence] recordings in
            home?.render(recordings: recordings)
            intelligence?.prepare(recordings: recordings)
        }
        deviceService.onFastTransferOffer = { [weak home] offer in home?.presentFastTransferOffer(offer) }
        deviceService.onFastTransferOfferDismissed = { [weak home] id in home?.dismissFastTransferOffer(id: id) }
        deviceService.onFastTransferStateChange = { [weak home] state in home?.render(fastTransferState: state) }
        deviceService.onMarkedMomentEvent = { [weak home] event in home?.render(markedMomentEvent: event) }
        intelligence.onSummaryChange = { [weak home, weak intelligence] _, _ in
            home?.render(summaryReadyTranscriptionIDs: intelligence?.summaryReadyTranscriptionIDs ?? [])
        }
        intelligence.onAutomaticStatusChange = { [weak home] message in
            home?.renderAutomaticSummaryStatus(message)
        }
        home.onManageRecorder = { [weak self] in
            guard let self, let session = self.currentSession else { return }
            self.showDeviceSetup(session: session)
        }
        home.onRetryRecording = { [weak self] recording in
            self?.deviceService.retryRecording(recording)
        }
        home.onRemoveRecorder = { [weak self, weak home] in
            self?.deviceService.removeRecorder { result in
                switch result {
                case .success:
                    guard let self, let session = self.currentSession else { return }
                    self.showDeviceSetup(session: session)
                case .failure(let error):
                    home?.showRecorderRemovalError(error.localizedDescription)
                }
            }
        }
        home.onOpenRecording = { [weak self, weak intelligence] recording in
            guard let self, let intelligence else { return }
            self.presentConversation(recording, intelligence: intelligence)
        }
        home.onHandoffRecording = { [weak self, weak intelligence] recording in
            guard let self, let intelligence else { return }
            self.presentAssistantHandoff(recording, intelligence: intelligence)
        }
        home.onOpenIntelligenceSettings = { [weak self, weak intelligence] in
            guard let self, let intelligence else { return }
            let settings = BetaIntelligenceSettingsViewController(
                client: intelligence.client,
                sessionTokenProvider: { [weak intelligence] in
                    intelligence?.session.sessionToken ?? ""
                }
            )
            settings.onSettingsChanged = { [weak intelligence, weak self] in
                intelligence?.reloadSettings { _ in
                    guard let recordings = self?.deviceService.recordings else { return }
                    intelligence?.prepare(recordings: recordings)
                }
            }
            let navigation = UINavigationController(rootViewController: settings)
            navigation.modalPresentationStyle = .formSheet
            self.navigationController.topViewController?.present(navigation, animated: true)
        }
        home.onManualFastTransfer = { [weak self] in self?.deviceService.requestManualFastTransfer() }
        home.onResolveFastTransferOffer = { [weak self] id, useWiFi in
            self?.deviceService.resolveFastTransferOffer(id: id, useWiFi: useWiFi)
        }
        home.onSignOut = { [weak self] in self?.signOut() }
        navigationController.setViewControllers([home], animated: true)
        home.render(fastTransferState: deviceService.fastTransferState)
        home.render(summaryReadyTranscriptionIDs: intelligence.summaryReadyTranscriptionIDs)
        intelligence.prepare(recordings: deviceService.recordings)
    }

    private func presentConversation(_ recording: BetaRecording, intelligence: BetaIntelligenceCoordinator) {
        let conversation = BetaConversationViewController(recording: recording, intelligence: intelligence)
        conversation.loadViewIfNeeded()
        conversation.renderMarkedMoments(deviceService.markedMoments(for: recording))
        conversation.onOpenTranscript = { [weak conversation] recording in
            let transcript = BetaTranscriptViewController(
                title: recording.title,
                transcript: recording.transcript ?? "No transcript text was returned."
            )
            transcript.modalPresentationStyle = .formSheet
            conversation?.present(transcript, animated: true)
        }
        conversation.onOpenAssistant = { [weak self, weak conversation] recording in
            guard let self else { return }
            self.presentAssistantHandoff(recording, intelligence: intelligence, presenter: conversation)
        }
        navigationController.topViewController?.present(conversation, animated: true)
    }

    private func presentAssistantHandoff(
        _ recording: BetaRecording,
        intelligence: BetaIntelligenceCoordinator,
        presenter: UIViewController? = nil
    ) {
        guard let userID = currentSession?.userID else { return }
        let host = presenter ?? navigationController.topViewController
        intelligence.loadSummary(for: recording) { [weak self, weak host] result in
            guard let self, let host else { return }
            let approvedSummary: String?
            if case .success(let summary) = result { approvedSummary = summary?.text }
            else { approvedSummary = intelligence.cachedSummary(for: recording.transcriptionID ?? "")?.text }
            let markedMoments = self.deviceService.markedMoments(for: recording)?.tags.map(\.timestamp) ?? []
            let handoff = BetaAgentHandoffViewController(
                recording: recording,
                userID: userID,
                approvedSummary: approvedSummary,
                markedMoments: markedMoments
            )
            host.present(handoff, animated: true)
        }
    }

    private func refreshSession(_ session: BetaSession, showFailure: Bool) {
        guard let apiClient else {
            if showFailure { showWelcome(blockingError: ConfigurationError.missingAPIBaseURL.localizedDescription) }
            return
        }
        guard !refreshInFlight, !accessValidationInFlight,
              !signOutInFlight, !signOutAwaitingResolution else { return }
        refreshInFlight = true
        let expectedUserID = session.userID
        let expectedSessionToken = session.sessionToken
        let expectedAuthOperationGeneration = authOperationGeneration
        apiClient.refresh(sessionToken: session.sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                guard let self,
                      self.authOperationGeneration == expectedAuthOperationGeneration else { return }
                self.refreshInFlight = false
                guard self.currentSession?.userID == expectedUserID,
                      self.currentSession?.sessionToken == expectedSessionToken else { return }
                switch result {
                case .success(let refreshed):
                    guard refreshed.userID == expectedUserID,
                          self.isTrusted(refreshed) else {
                        self.accessResumePending = true
                        self.refreshTimer?.invalidate()
                        self.refreshTimer = nil
                        self.deviceService.suspendForAccessValidation(
                            message: "PinPoint received an untrusted session refresh, so automatic sync is paused."
                        )
                        self.presentAccessValidationFailure(
                            error: ConfigurationError.invalidPlaudDomain
                        )
                        return
                    }
                    do {
                        let persisted = try self.persistSession(refreshed)
                        self.currentSession = persisted
                        if self.resumeClearsPendingSignOut {
                            guard self.clearPendingSignOut() else {
                                self.accessResumePending = true
                                self.presentAccessValidationFailure(
                                    error: PendingSignOutError.checkpointUnavailable
                                )
                                return
                            }
                            self.resumeClearsPendingSignOut = false
                            self.signOutAwaitingResolution = false
                        }
                        self.lastAccessValidationAt = Date()
                        self.scheduleSessionRefresh(for: persisted)
                        self.scheduleAccessValidation()
                        self.deviceService.updateSession(persisted)
                        self.intelligenceCoordinator?.updateSession(persisted)
                        if self.accessResumePending,
                           !self.signOutInFlight,
                           !self.signOutAwaitingResolution {
                            self.accessResumePending = false
                            self.deviceService.resumeAfterAccessValidation()
                        }
                    } catch {
                        self.accessResumePending = true
                        self.refreshTimer?.invalidate()
                        self.refreshTimer = nil
                        self.deviceService.suspendForAccessValidation(
                            message: "PinPoint could not safely save the refreshed sign-in."
                        )
                        self.presentAccessValidationFailure(error: error)
                        return
                    }
                    if (showFailure || self.bootstrapPending), let persisted = self.currentSession {
                        self.bootstrapPending = false
                        self.showDeviceSetup(session: persisted)
                    }
                case .failure(let error):
                    if case PinpointAPIError.sessionExpired = error {
                        self.expireCurrentSession(message: error.localizedDescription)
                    } else {
                        if self.accessResumePending || self.bootstrapPending {
                            self.refreshTimer?.invalidate()
                            self.refreshTimer = nil
                            self.accessResumePending = true
                            self.presentAccessValidationFailure(error: error)
                        } else {
                            self.scheduleSessionRefreshRetry(for: session)
                            if showFailure { self.showWelcome(initialError: error.localizedDescription) }
                        }
                    }
                }
            }
        }
    }

    private func signOut() {
        guard !signOutInFlight, let session = currentSession, let apiClient else { return }
        authOperationGeneration &+= 1
        let expectedAuthOperationGeneration = authOperationGeneration
        refreshInFlight = false
        accessValidationInFlight = false
        accessFailureAlert?.dismiss(animated: false)
        accessFailureAlert = nil
        accessValidationTimer?.invalidate()
        accessValidationTimer = nil
        refreshTimer?.invalidate()
        refreshTimer = nil
        accessResumePending = false
        deviceService.suspendForAccessValidation(
            message: "PinPoint disconnected the recorder while it confirms sign-out."
        )
        guard persistPendingSignOut() else {
            signOutAwaitingResolution = true
            presentUnconfirmedSignOut(error: PendingSignOutError.checkpointUnavailable)
            return
        }
        signOutInFlight = true
        apiClient.revokeSession(sessionToken: session.sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                guard let self,
                      self.authOperationGeneration == expectedAuthOperationGeneration,
                      self.currentSession?.sessionToken == session.sessionToken else { return }
                self.signOutInFlight = false
                switch result {
                case .success:
                    self.signOutAwaitingResolution = false
                    self.finishLocalSignOut()
                case .failure(PinpointAPIError.sessionExpired):
                    // A 401 means the server-side session is already unusable.
                    // Finish the local half instead of leaving the SDK alive.
                    self.signOutAwaitingResolution = false
                    self.finishLocalSignOut()
                case .failure(let error):
                    self.signOutAwaitingResolution = true
                    self.presentUnconfirmedSignOut(error: error)
                }
            }
        }
    }

    private func presentUnconfirmedSignOut(error: Error) {
        if accessFailureAlert?.presentingViewController != nil { return }
        let alert = UIAlertController(
            title: "Automatic sync is paused",
            message: error.localizedDescription + "\n\nThe recorder is disconnected and nothing new will sync until you retry sign-out or confirm that this sign-in is still active.",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "Retry Sign Out", style: .destructive) { [weak self] _ in
            self?.accessFailureAlert = nil
            self?.signOut()
        })
        alert.addAction(UIAlertAction(title: "Resume PinPoint", style: .default) { [weak self] _ in
            guard let self, let session = self.currentSession else { return }
            self.accessFailureAlert = nil
            self.signOutAwaitingResolution = false
            self.resumeClearsPendingSignOut = true
            self.accessResumePending = true
            if self.bootstrapPending {
                self.refreshSession(session, showFailure: true)
            } else {
                self.validateAccess(
                    session,
                    suspendBeforeRequest: false,
                    pauseMessage: "Confirming this sign-in before automatic sync resumes."
                )
            }
        })
        accessFailureAlert = alert
        navigationController.topViewController?.present(alert, animated: true)
    }

    private func finishLocalSignOut() {
        // The backend session is already revoked. Stop Plaud/BLE immediately;
        // a Keychain failure must never leave automatic copy running with the
        // still-cached Plaud bearer token.
        authOperationGeneration &+= 1
        refreshTimer?.invalidate()
        refreshTimer = nil
        accessValidationTimer?.invalidate()
        accessValidationTimer = nil
        refreshInFlight = false
        accessValidationInFlight = false
        lastAccessValidationAt = nil
        accessResumePending = false
        signOutInFlight = false
        signOutAwaitingResolution = false
        deviceService.disconnect()
        intelligenceCoordinator = nil
        currentSession = nil
        do {
            try sessionStore.clear()
            guard try sessionStore.load() == nil else {
                throw SessionStoreError.invalidSession
            }
        } catch {
            navigationController.setViewControllers([
                BetaRestartViewController(
                    titleText: "Signed out, but PinPoint could not clear Keychain",
                    detailText: "Automatic sync is stopped. Press ⌘Q, reopen PinPoint, and sign out again before using another account.",
                    symbolName: "exclamationmark.shield.fill",
                    noteText: "The server sign-in was revoked. PinPoint will not reconnect the recorder in this process."
                )
            ], animated: true)
            return
        }
        guard clearPendingSignOut() else {
            navigationController.setViewControllers([
                BetaRestartViewController(
                    titleText: "Signed out — restart PinPoint",
                    detailText: "The server sign-in and Keychain session were cleared, but PinPoint could not clear its local sign-out checkpoint.",
                    symbolName: "exclamationmark.shield.fill",
                    noteText: "Automatic sync remains stopped. Press ⌘Q and reopen PinPoint to finish local cleanup."
                )
            ], animated: true)
            return
        }
        resumeClearsPendingSignOut = false
        bootstrapPending = false
        navigationController.setViewControllers([BetaRestartViewController()], animated: true)
    }

    private func confirmLocalDataDeletion() {
        let alert = UIAlertController(
            title: "Delete local audio and transcripts?",
            message: "This removes every PinPoint recording copied for the current account from this Mac. Release the recorder first so those files cannot be copied back. Plaud-hosted content is not deleted.",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Delete local data", style: .destructive) { [weak self] _ in
            guard let self else { return }
            do {
                try self.deviceService.deleteCurrentUserLocalData()
                let done = UIAlertController(
                    title: "Local data deleted",
                    message: "Copied PinPoint audio, transcripts, and recording metadata were removed from this Mac.",
                    preferredStyle: .alert
                )
                done.addAction(UIAlertAction(title: "Done", style: .default))
                self.navigationController.topViewController?.present(done, animated: true)
            } catch {
                let title: String
                if case BetaRecordingStoreError.localDataDeletedButStorageUnavailable = error {
                    title = "Local data deleted — restart PinPoint"
                } else {
                    title = "Local data was not deleted"
                }
                let failure = UIAlertController(
                    title: title,
                    message: error.localizedDescription,
                    preferredStyle: .alert
                )
                failure.addAction(UIAlertAction(title: "OK", style: .default))
                self.navigationController.topViewController?.present(failure, animated: true)
            }
        })
        navigationController.topViewController?.present(alert, animated: true)
    }

    private var hasPendingSignOut: Bool {
        defaults.bool(forKey: Keys.pendingSignOut)
    }

    private func persistPendingSignOut() -> Bool {
        defaults.set(true, forKey: Keys.pendingSignOut)
        return defaults.synchronize() && defaults.bool(forKey: Keys.pendingSignOut)
    }

    private func clearPendingSignOut() -> Bool {
        defaults.removeObject(forKey: Keys.pendingSignOut)
        return defaults.synchronize() && defaults.object(forKey: Keys.pendingSignOut) == nil
    }

    private func showPendingSignOutStorageFailure() {
        navigationController.setViewControllers([
            BetaRestartViewController(
                titleText: "PinPoint needs a restart",
                detailText: "Automatic sync is stopped, but PinPoint could not finish clearing its local sign-out checkpoint.",
                symbolName: "exclamationmark.shield.fill",
                noteText: "Press ⌘Q, check that this Mac has free disk space, and reopen PinPoint."
            )
        ], animated: false)
    }

    private func showSessionStorageFailure(detail: String) {
        navigationController.setViewControllers([
            BetaRestartViewController(
                titleText: "PinPoint cannot safely open this sign-in",
                detailText: detail,
                symbolName: "exclamationmark.shield.fill",
                noteText: "Press ⌘Q, check Keychain and free disk space, then reopen PinPoint. Contact the service operator if it repeats."
            )
        ], animated: false)
    }

    private func isTrusted(_ session: BetaSession) -> Bool {
        guard let configuration else { return false }
        return session.plaudDomain == configuration.plaudDomain
            && session.deploymentMode == configuration.deploymentMode
            && session.userID.hasPrefix("pinpoint_")
            && !session.sessionToken.isEmpty
            && !session.plaudUserAccessToken.isEmpty
    }

    private func persistSession(_ session: BetaSession) throws -> BetaSession {
        try sessionStore.save(session)
        guard let persisted = try sessionStore.load(),
              persisted.sessionToken == session.sessionToken,
              persisted.plaudUserAccessToken == session.plaudUserAccessToken,
              persisted.userID == session.userID,
              persisted.plaudDomain == session.plaudDomain,
              persisted.deploymentMode == session.deploymentMode,
              abs(persisted.sessionExpiresAt.timeIntervalSince(session.sessionExpiresAt)) < 1.1,
              abs(persisted.plaudTokenExpiresAt.timeIntervalSince(session.plaudTokenExpiresAt)) < 1.1 else {
            throw SessionStoreError.invalidSession
        }
        // Use the canonical Keychain round-trip value so in-memory and relaunch
        // scheduling observe the same whole-second ISO-8601 timestamps.
        return persisted
    }

    private func scheduleSessionRefresh(for session: BetaSession) {
        refreshTimer?.invalidate()
        let nextExpiry = min(session.sessionExpiresAt, session.plaudTokenExpiresAt)
        let delay = max(1, nextExpiry.timeIntervalSinceNow - 5 * 60)
        refreshTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
            guard let self, self.currentSession?.userID == session.userID else { return }
            self.refreshSession(session, showFailure: false)
        }
    }

    private func scheduleSessionRefreshRetry(for session: BetaSession) {
        refreshTimer?.invalidate()
        guard session.sessionExpiresAt.timeIntervalSinceNow > 60 else {
            let delay = max(1, session.sessionExpiresAt.timeIntervalSinceNow + 1)
            refreshTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
                guard let self, self.currentSession?.sessionToken == session.sessionToken else { return }
                self.expireCurrentSession(message: PinpointAPIError.sessionExpired.localizedDescription)
            }
            return
        }
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: false) { [weak self] _ in
            guard let self, self.currentSession?.sessionToken == session.sessionToken else { return }
            self.refreshSession(session, showFailure: false)
        }
    }

    private func refreshAfterServiceExpiry() {
        guard let session = currentSession else { return }
        accessResumePending = true
        refreshTimer?.invalidate()
        refreshTimer = nil
        deviceService.suspendForAccessValidation(
            message: "PinPoint is refreshing an expired service session before automatic sync resumes."
        )
        guard !refreshInFlight else { return }
        refreshSession(session, showFailure: false)
    }

    private func scheduleAccessValidation() {
        accessValidationTimer?.invalidate()
        guard let session = currentSession,
              !signOutInFlight,
              !signOutAwaitingResolution else { return }
        let elapsed = lastAccessValidationAt.map { Date().timeIntervalSince($0) } ?? accessValidationInterval
        let delay = max(1, accessValidationInterval - elapsed)
        accessValidationTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
            guard let self,
                  self.currentSession?.sessionToken == session.sessionToken else { return }
            self.validateAccess(
                session,
                suspendBeforeRequest: true,
                pauseMessage: "PinPoint could not confirm this sign-in, so automatic sync is paused."
            )
        }
    }

    private func validateAccessAfterLeaseExpiry() {
        guard let session = currentSession,
              !signOutInFlight,
              !signOutAwaitingResolution else { return }
        accessResumePending = true
        refreshTimer?.invalidate()
        refreshTimer = nil
        validateAccess(
            session,
            suspendBeforeRequest: false,
            pauseMessage: "Confirming that this PinPoint sign-in is still active before automatic sync resumes."
        )
    }

    private func validateAccess(
        _ session: BetaSession,
        suspendBeforeRequest: Bool,
        pauseMessage: String
    ) {
        guard let apiClient, !signOutInFlight else { return }
        if refreshInFlight {
            let validationIsDue = lastAccessValidationAt.map {
                Date().timeIntervalSince($0) >= accessValidationInterval
            } ?? true
            if suspendBeforeRequest || validationIsDue {
                accessValidationTimer?.invalidate()
                accessValidationTimer = nil
                refreshTimer?.invalidate()
                refreshTimer = nil
                accessResumePending = true
                deviceService.suspendForAccessValidation(message: pauseMessage)
            }
            // The membership-gated refresh already in flight becomes the
            // access decision. Its callback must explicitly resume or alert.
            return
        }
        guard !accessValidationInFlight else { return }
        if suspendBeforeRequest {
            refreshTimer?.invalidate()
            refreshTimer = nil
            accessResumePending = true
            deviceService.suspendForAccessValidation(message: pauseMessage)
        }
        accessValidationTimer?.invalidate()
        accessValidationTimer = nil
        accessValidationInFlight = true
        let expectedUserID = session.userID
        let expectedSessionToken = session.sessionToken
        let expectedAuthOperationGeneration = authOperationGeneration
        apiClient.validateSession(sessionToken: expectedSessionToken) { [weak self] result in
            DispatchQueue.main.async {
                guard let self,
                      self.authOperationGeneration == expectedAuthOperationGeneration else { return }
                self.accessValidationInFlight = false
                guard self.currentSession?.userID == expectedUserID,
                      self.currentSession?.sessionToken == expectedSessionToken else { return }
                switch result {
                case .success:
                    if self.hasPendingSignOut && !self.resumeClearsPendingSignOut {
                        self.accessResumePending = true
                        self.signOutAwaitingResolution = true
                        self.deviceService.suspendForAccessValidation(
                            message: "PinPoint is waiting for you to finish or explicitly reverse sign-out."
                        )
                        self.accessFailureAlert?.dismiss(animated: false)
                        self.accessFailureAlert = nil
                        self.presentUnconfirmedSignOut(error: PendingSignOutError.awaitingResolution)
                        return
                    }
                    if self.resumeClearsPendingSignOut {
                        guard self.clearPendingSignOut() else {
                            self.accessResumePending = true
                            self.presentAccessValidationFailure(
                                error: PendingSignOutError.checkpointUnavailable
                            )
                            return
                        }
                        self.resumeClearsPendingSignOut = false
                        self.signOutAwaitingResolution = false
                    }
                    self.signOutAwaitingResolution = false
                    self.lastAccessValidationAt = Date()
                    if self.accessResumePending {
                        self.accessResumePending = false
                        self.deviceService.resumeAfterAccessValidation()
                    }
                    self.scheduleAccessValidation()
                    let nextExpiry = min(session.sessionExpiresAt, session.plaudTokenExpiresAt)
                    if nextExpiry.timeIntervalSinceNow <= 5 * 60 {
                        self.refreshSession(session, showFailure: false)
                    } else {
                        self.scheduleSessionRefresh(for: session)
                    }
                case .failure(PinpointAPIError.sessionExpired):
                    self.accessResumePending = true
                    self.deviceService.suspendForAccessValidation(
                        message: "This PinPoint sign-in is no longer active."
                    )
                    if self.signOutAwaitingResolution
                        || self.resumeClearsPendingSignOut
                        || self.hasPendingSignOut {
                        self.signOutAwaitingResolution = false
                        self.finishLocalSignOut()
                    } else {
                        self.expireCurrentSession(message: "This PinPoint invitation or session is no longer active.")
                    }
                case .failure(let error):
                    self.refreshTimer?.invalidate()
                    self.refreshTimer = nil
                    self.accessResumePending = true
                    self.deviceService.suspendForAccessValidation(message: pauseMessage)
                    self.presentAccessValidationFailure(error: error)
                }
            }
        }
    }

    private func presentAccessValidationFailure(error: Error) {
        if accessFailureAlert?.presentingViewController != nil { return }
        let alert = UIAlertController(
            title: "Automatic sync is paused",
            message: error.localizedDescription + "\n\nPinPoint disconnected the recorder because it could not confirm this sign-in.",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "Retry Check", style: .default) { [weak self] _ in
            guard let self, let session = self.currentSession else { return }
            self.accessFailureAlert = nil
            if self.bootstrapPending {
                self.refreshSession(session, showFailure: true)
            } else {
                self.validateAccess(
                    session,
                    suspendBeforeRequest: false,
                    pauseMessage: "Confirming this PinPoint sign-in before automatic sync resumes."
                )
            }
        })
        alert.addAction(UIAlertAction(title: "Sign Out", style: .destructive) { [weak self] _ in
            self?.accessFailureAlert = nil
            self?.signOut()
        })
        accessFailureAlert = alert
        navigationController.topViewController?.present(alert, animated: true)
    }

    private func expireCurrentSession(message: String) {
        authOperationGeneration &+= 1
        refreshTimer?.invalidate()
        refreshTimer = nil
        accessValidationTimer?.invalidate()
        accessValidationTimer = nil
        refreshInFlight = false
        accessValidationInFlight = false
        lastAccessValidationAt = nil
        accessResumePending = false
        signOutInFlight = false
        signOutAwaitingResolution = false
        deviceService.suspendForAccessValidation(message: message)
        do {
            try sessionStore.clear()
            guard try sessionStore.load() == nil else {
                throw SessionStoreError.invalidSession
            }
        } catch {
            pendingExpiryMessage = message
            deviceService.disconnect()
            navigationController.setViewControllers([
                BetaRestartViewController(
                    titleText: "PinPoint could not clear the expired sign-in",
                    detailText: "Quit and reopen PinPoint, then try again. The recorder stays disconnected until the saved sign-in can be removed safely.",
                    symbolName: "exclamationmark.shield.fill"
                )
            ], animated: true)
            return
        }
        pendingExpiryMessage = nil
        currentSession = nil
        if deviceService.requiresColdStartForNewUser {
            deviceService.disconnect()
            navigationController.setViewControllers([
                BetaRestartViewController(
                    titleText: "Your sign-in expired",
                    detailText: "Press ⌘Q, then reopen PinPoint and sign in again. This cold start keeps Plaud from carrying the previous recorder owner into the new session.",
                    symbolName: "clock.arrow.circlepath"
                )
            ], animated: true)
        } else {
            showWelcome(initialError: message)
        }
    }
}

private enum PendingSignOutError: LocalizedError {
    case awaitingResolution
    case checkpointUnavailable

    var errorDescription: String? {
        switch self {
        case .awaitingResolution:
            return "The previous sign-out did not receive a confirmed server response."
        case .checkpointUnavailable:
            return "PinPoint could not safely update its local sign-out checkpoint. Check disk space and try again."
        }
    }
}
