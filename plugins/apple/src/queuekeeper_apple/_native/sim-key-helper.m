// Development tooling only: never linked into an app, a package or a gate. scripts/sim-key builds
// it for this Mac, signs it ad hoc and runs it on the host, never inside the simulator.
//
// Sends hardware-keyboard key-down and key-up events to a booted iOS simulator, as a Mac keyboard
// attached to Simulator.app sends them, with no Simulator window open. CoreSimulator's
// SimServiceContext finds the device by its UDID in the developer directory's default device set;
// SimulatorKit's SimDeviceLegacyHIDClient opens the device's HID session; and each key event is an
// Indigo HID message from SimulatorKit's IndigoHIDMessageForKeyboardArbitrary, a keyboard usage
// (HID usage page 7) and a direction, 1 down and 2 up. Private, undocumented host API, which any
// Xcode can break: the approach is that of Meta's idb (https://github.com/facebook/idb), MIT
// licensed, in its FBSimulatorHID and FBSimulatorIndigoHID. This file is written fresh from that
// approach and shares only the private interface's names and call sequence with idb, whose
// notice follows.
//
//   sim-key-helper DEVELOPER_DIR UDID EVENT...    each EVENT is down:USAGE or up:USAGE, in decimal
//
// Exit status: 0 once every event is sent; 2 when UDID is not a booted simulator of the default
// device set; 3 when the HID service cannot be reached (a framework, class or symbol is missing, the
// HID client does not open, or a send fails or does not finish), so that a caller can report its
// measurement as not taken; 64 for arguments it does not take.
//
// MIT License
//
// Copyright (c) Meta Platforms, Inc. and affiliates.
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.

#import <Foundation/Foundation.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

enum {
    kExitNoDevice = 2,
    kExitUnreachable = 3,
    kExitUsage = 64,
    kDirectionDown = 1,
    kDirectionUp = 2,
    kSendTimeoutSeconds = 5,
    // A pause after each event, so that a key's down and up are not coalesced.
    kPauseMicroseconds = 50000,
};

static const char *const kCoreSimulator =
    "/Library/Developer/PrivateFrameworks/CoreSimulator.framework/CoreSimulator";
static NSString *const kSimulatorKit =
    @"../SharedFrameworks/SimulatorKit.framework/SimulatorKit";

// The private methods used, declared so that the compiler knows their signatures.
@protocol QKSimServiceContext
+ (id)sharedServiceContextForDeveloperDir:(NSString *)developerDir error:(NSError **)error;
- (id)defaultDeviceSetWithError:(NSError **)error;
@end
@protocol QKSimDeviceSet
- (NSDictionary *)devicesByUDID;
@end
@protocol QKSimDevice
- (NSString *)stateString;
@end
@protocol QKSimDeviceLegacyHIDClient
- (id)initWithDevice:(id)device error:(NSError **)error;
- (void)sendWithMessage:(void *)message
           freeWhenDone:(BOOL)freeWhenDone
        completionQueue:(dispatch_queue_t)queue
             completion:(void (^)(NSError *error))completion;
@end

typedef void *(*KeyboardMessage)(uint32_t usage, uint32_t direction);

typedef struct {
    uint32_t direction;
    uint32_t usage;
} Event;

static int usage(void) {
    fprintf(stderr, "usage: sim-key-helper DEVELOPER_DIR UDID down:USAGE|up:USAGE...\n");
    return kExitUsage;
}

static int unreachable(NSString *what, NSError *error) {
    fprintf(stderr, "sim-key-helper: the simulator's HID service cannot be reached: %s%s%s\n",
            what.UTF8String, error ? ": " : "", error ? error.localizedDescription.UTF8String : "");
    return kExitUnreachable;
}

// Reads down:USAGE or up:USAGE, USAGE a decimal keyboard usage from 1 to 255.
static BOOL parse(const char *text, Event *event) {
    const char *rest;
    if (strncmp(text, "down:", 5) == 0) {
        event->direction = kDirectionDown;
        rest = text + 5;
    } else if (strncmp(text, "up:", 3) == 0) {
        event->direction = kDirectionUp;
        rest = text + 3;
    } else {
        return NO;
    }
    if (*rest < '1' || *rest > '9') return NO;
    char *end = NULL;
    unsigned long value = strtoul(rest, &end, 10);
    if (*end != '\0' || value > 255) return NO;
    event->usage = (uint32_t)value;
    return YES;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc < 4 || argv[1][0] != '/') return usage();
        NSUUID *udid = [[NSUUID alloc] initWithUUIDString:@(argv[2])];
        if (udid == nil) return usage();
        int count = argc - 3;
        Event *events = calloc((size_t)count, sizeof *events);
        for (int i = 0; i < count; i++) {
            if (!parse(argv[i + 3], &events[i])) return usage();
        }
        NSString *developerDir = @(argv[1]);

        if (dlopen(kCoreSimulator, RTLD_NOW) == NULL) {
            return unreachable([NSString stringWithFormat:@"CoreSimulator did not load (%s)", dlerror()], nil);
        }
        NSString *kit = [[developerDir stringByAppendingPathComponent:kSimulatorKit] stringByStandardizingPath];
        void *simulatorKit = dlopen(kit.fileSystemRepresentation, RTLD_NOW);
        if (simulatorKit == NULL) {
            return unreachable([NSString stringWithFormat:@"SimulatorKit did not load (%s)", dlerror()], nil);
        }
        KeyboardMessage keyboardMessage =
            (KeyboardMessage)dlsym(simulatorKit, "IndigoHIDMessageForKeyboardArbitrary");
        if (keyboardMessage == NULL) return unreachable(@"no IndigoHIDMessageForKeyboardArbitrary", nil);
        Class contextClass = NSClassFromString(@"SimServiceContext");
        Class clientClass = NSClassFromString(@"SimulatorKit.SimDeviceLegacyHIDClient");
        if (contextClass == nil) return unreachable(@"no SimServiceContext class", nil);
        if (clientClass == nil) return unreachable(@"no SimDeviceLegacyHIDClient class", nil);

        NSError *error = nil;
        id<QKSimServiceContext> context =
            [(id<QKSimServiceContext>)contextClass sharedServiceContextForDeveloperDir:developerDir error:&error];
        if (context == nil) return unreachable(@"no CoreSimulator service context", error);
        id<QKSimDeviceSet> set = [context defaultDeviceSetWithError:&error];
        if (set == nil) return unreachable(@"no default device set", error);
        id<QKSimDevice> device = set.devicesByUDID[udid];
        if (device == nil) {
            fprintf(stderr, "sim-key-helper: no simulator %s in the default device set\n", argv[2]);
            return kExitNoDevice;
        }
        if (![device.stateString isEqualToString:@"Booted"]) {
            fprintf(stderr, "sim-key-helper: simulator %s is %s, not booted\n", argv[2],
                    device.stateString.UTF8String);
            return kExitNoDevice;
        }
        id<QKSimDeviceLegacyHIDClient> client =
            [(id<QKSimDeviceLegacyHIDClient>)[clientClass alloc] initWithDevice:device error:&error];
        if (client == nil) return unreachable(@"the HID client did not open", error);

        dispatch_queue_t queue = dispatch_queue_create("sim-key-helper", DISPATCH_QUEUE_SERIAL);
        for (int i = 0; i < count; i++) {
            void *message = keyboardMessage(events[i].usage, events[i].direction);
            if (message == NULL) return unreachable(@"no keyboard message", nil);
            dispatch_semaphore_t sent = dispatch_semaphore_create(0);
            __block NSError *failure = nil;
            [client sendWithMessage:message
                       freeWhenDone:YES
                    completionQueue:queue
                         completion:^(NSError *sendError) {
                             failure = sendError;
                             dispatch_semaphore_signal(sent);
                         }];
            dispatch_time_t limit = dispatch_time(DISPATCH_TIME_NOW, kSendTimeoutSeconds * NSEC_PER_SEC);
            if (dispatch_semaphore_wait(sent, limit) != 0) {
                return unreachable(@"a send did not finish in time", nil);
            }
            if (failure != nil) return unreachable(@"a send failed", failure);
            usleep(kPauseMicroseconds);
        }
        free(events);
        return 0;
    }
}
