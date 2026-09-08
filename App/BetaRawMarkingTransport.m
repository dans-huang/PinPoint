#import "BetaRawMarkingTransport.h"
#import <objc/runtime.h>

@interface BetaRawMarkingTransport ()
@property(nonatomic, strong) BleAgent *agent;
@property(nonatomic, weak) id<BleAgentProtocol> primaryDelegate;
@property(nonatomic, copy, nullable) NSString *activeScopeIdentifier;
@property(nonatomic, copy, nullable) BetaRawMarkingTagsHandler tagsHandler;
@property(nonatomic, copy, nullable) BetaRawLegacyMarkingHandler legacyHandler;
@end

@implementation BetaRawMarkingTransport

- (void)dealloc {
    [self restorePrimaryDelegate];
}

- (BOOL)respondsToSelector:(SEL)selector {
    if ([super respondsToSelector:selector]) return YES;
    Protocol *protocol = @protocol(BleAgentProtocol);
    struct objc_method_description required = protocol_getMethodDescription(protocol, selector, YES, YES);
    struct objc_method_description optional = protocol_getMethodDescription(protocol, selector, NO, YES);
    return required.name != NULL || optional.name != NULL;
}

- (NSMethodSignature *)methodSignatureForSelector:(SEL)selector {
    NSMethodSignature *signature = [super methodSignatureForSelector:selector];
    if (signature) return signature;
    Protocol *protocol = @protocol(BleAgentProtocol);
    struct objc_method_description description = protocol_getMethodDescription(protocol, selector, YES, YES);
    if (description.name == NULL) {
        description = protocol_getMethodDescription(protocol, selector, NO, YES);
    }
    return description.name == NULL
        ? nil
        : [NSMethodSignature signatureWithObjCTypes:description.types];
}

- (void)forwardInvocation:(NSInvocation *)invocation {
    id primary = self.primaryDelegate;
    if (primary != nil && [primary respondsToSelector:invocation.selector]) {
        [invocation invokeWithTarget:primary];
        return;
    }
    NSUInteger length = invocation.methodSignature.methodReturnLength;
    if (length > 0) {
        void *zero = calloc(1, length);
        [invocation setReturnValue:zero];
        free(zero);
    }
}

- (void)beginCaptureWithScopeIdentifier:(NSString *)scopeIdentifier
                            tagsHandler:(BetaRawMarkingTagsHandler)tagsHandler
                          legacyHandler:(BetaRawLegacyMarkingHandler)legacyHandler {
    if (scopeIdentifier.length == 0) return;
    if (self.activeScopeIdentifier != nil
        && ![self.activeScopeIdentifier isEqualToString:scopeIdentifier]) {
        [self restorePrimaryDelegate];
    }
    self.agent = [BleAgent shared];
    if (self.agent.delegate != (id<BleAgentProtocol>)self) {
        self.primaryDelegate = self.agent.delegate;
        self.agent.delegate = (id<BleAgentProtocol>)self;
    }
    self.activeScopeIdentifier = [scopeIdentifier copy];
    self.tagsHandler = [tagsHandler copy];
    self.legacyHandler = [legacyHandler copy];
}

- (void)endCaptureWithScopeIdentifier:(NSString *)scopeIdentifier {
    if (![self.activeScopeIdentifier isEqualToString:scopeIdentifier]) return;
    [self restorePrimaryDelegate];
}

- (void)requestRecordMarkingTagsWithScopeIdentifier:(NSString *)scopeIdentifier
                                                 uid:(NSInteger)uid
                                      startTimestamp:(NSInteger)startTimestamp
                                        endTimestamp:(NSInteger)endTimestamp {
    if (![self.activeScopeIdentifier isEqualToString:scopeIdentifier]) return;
    if (self.agent.delegate != (id<BleAgentProtocol>)self) return;
    [self.agent getRecordMarkingTagsWithUid:uid
                             startTimestamp:startTimestamp
                               endTimestamp:endTimestamp];
}

- (void)requestLegacyMarkingWithScopeIdentifier:(NSString *)scopeIdentifier
                                      sessionId:(NSInteger)sessionId {
    if (![self.activeScopeIdentifier isEqualToString:scopeIdentifier]) return;
    if (self.agent.delegate != (id<BleAgentProtocol>)self) return;
    [self.agent getMarking:sessionId];
}

- (void)bleGetRecordMarkingTagsWithUid:(NSInteger)uid
                                totals:(NSInteger)totals
                                 index:(NSInteger)index
                                  tags:(NSArray<BleRecordMarkingTag *> *)tags {
    id<BleAgentProtocol> primary = self.primaryDelegate;
    if (primary != nil && [(id)primary respondsToSelector:_cmd]) {
        [primary bleGetRecordMarkingTagsWithUid:uid totals:totals index:index tags:tags];
    }
    NSString *scope = self.activeScopeIdentifier;
    BetaRawMarkingTagsHandler handler = self.tagsHandler;
    if (scope != nil && handler != nil) handler(scope, uid, totals, index, tags);
}

- (void)bleMarkingWithSessionId:(NSInteger)sessionId
                         status:(NSInteger)status
                       markList:(NSArray<NSNumber *> *)markList {
    id<BleAgentProtocol> primary = self.primaryDelegate;
    if (primary != nil && [(id)primary respondsToSelector:_cmd]) {
        [primary bleMarkingWithSessionId:sessionId status:status markList:markList];
    }
    NSString *scope = self.activeScopeIdentifier;
    BetaRawLegacyMarkingHandler handler = self.legacyHandler;
    if (scope != nil && handler != nil) handler(scope, sessionId, status, markList);
}

- (void)restorePrimaryDelegate {
    if (self.agent.delegate == (id<BleAgentProtocol>)self) {
        self.agent.delegate = self.primaryDelegate;
    }
    self.activeScopeIdentifier = nil;
    self.tagsHandler = nil;
    self.legacyHandler = nil;
    self.primaryDelegate = nil;
}

@end
