#import <Foundation/Foundation.h>
#import <PlaudBleSDK/PlaudBleSDK-Swift.h>

NS_ASSUME_NONNULL_BEGIN

typedef void (^BetaRawMarkingTagsHandler)(
    NSString *scopeIdentifier,
    NSInteger uid,
    NSInteger totals,
    NSInteger index,
    NSArray<BleRecordMarkingTag *> *tags
);

typedef void (^BetaRawLegacyMarkingHandler)(
    NSString *scopeIdentifier,
    NSInteger sessionId,
    NSInteger status,
    NSArray<NSNumber *> *markList
);

/// A narrowly scoped delegate relay for the two official read-only marking
/// commands. It never issues delete, transfer, bind, depair, or record commands.
/// Every unrelated callback is forwarded to PlaudDeviceAgent's original
/// low-level delegate so the Beta connection and copy pipeline keep running.
@interface BetaRawMarkingTransport : NSObject

- (void)beginCaptureWithScopeIdentifier:(NSString *)scopeIdentifier
                            tagsHandler:(BetaRawMarkingTagsHandler)tagsHandler
                          legacyHandler:(BetaRawLegacyMarkingHandler)legacyHandler;
- (void)endCaptureWithScopeIdentifier:(NSString *)scopeIdentifier;
- (void)requestRecordMarkingTagsWithScopeIdentifier:(NSString *)scopeIdentifier
                                                 uid:(NSInteger)uid
                                      startTimestamp:(NSInteger)startTimestamp
                                        endTimestamp:(NSInteger)endTimestamp;
- (void)requestLegacyMarkingWithScopeIdentifier:(NSString *)scopeIdentifier
                                      sessionId:(NSInteger)sessionId;

@end

NS_ASSUME_NONNULL_END
