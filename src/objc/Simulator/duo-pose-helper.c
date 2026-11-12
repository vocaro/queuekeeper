// Development tooling only: never linked into an app, a package or a gate. scripts/duo-pose
// builds it against the iOS Simulator SDK, signs it ad hoc and runs it inside a booted iPhone Duo
// simulator with `xcrun simctl spawn` (guides/simulator-testing.md#setting-a-pose).
//
// Posts the simulated device-state event that Device Hub's fold and orientation controls post: a
// vendor-defined HID event (usage page 0xFF61, usage 0x5B) whose data is a binary-serialized
// dictionary naming the control and its value. The simulator's device-state relay turns it into a
// hinge angle or a device orientation. Private, undocumented simulator API, which any Xcode or
// runtime can break: the event format is as the artemnovichkov/hinge
// (https://github.com/artemnovichkov/hinge) and skarol/duoctl (https://github.com/skarol/duoctl,
// Copyright (c) 2026 Karolina Samorek) projects, both MIT licensed, describe it. This file is
// written from that description and shares only the private interface's declarations and call
// sequence with artemnovichkov/hinge's hinge_helper.c, whose notice follows.
//
//   duo-pose-helper hinge <degrees, 0 closed to 180 flat>
//   duo-pose-helper orientation <portrait|pud|landscape-left|landscape-right>
//
// MIT License
//
// Copyright (c) 2026 Artem Novichkov
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

#include <CoreFoundation/CoreFoundation.h>
#include <mach/mach_time.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct __IOHIDEvent *IOHIDEventRef;
typedef struct __IOHIDEventSystemClient *IOHIDEventSystemClientRef;

extern IOHIDEventRef IOHIDEventCreateVendorDefinedEvent(
    CFAllocatorRef allocator, uint64_t timeStamp, uint32_t usagePage, uint32_t usage,
    uint32_t version, const uint8_t *data, CFIndex length, uint32_t options);
extern IOHIDEventSystemClientRef IOHIDEventSystemClientCreateWithType(
    CFAllocatorRef allocator, int type, CFDictionaryRef properties);
extern void IOHIDEventSystemClientDispatchEvent(
    IOHIDEventSystemClientRef client, IOHIDEventRef event);
extern CFDataRef IOCFSerialize(CFTypeRef object, CFOptionFlags options);

enum {
    kDeviceStateUsagePage = 0xFF61,
    kDeviceStateUsage = 0x5B,
    kEventSystemClientSimple = 4,
    kSerializeBinary = 1,
};

static const char *const kOrientations[] = {"portrait", "pud", "landscape-left", "landscape-right"};

static int usage(void) {
    fprintf(stderr,
            "usage: duo-pose-helper hinge <degrees 0-180>\n"
            "       duo-pose-helper orientation <portrait|pud|landscape-left|landscape-right>\n");
    return 64;
}

// Posts one device-state event for `source` with `value`, and returns 0 once it is dispatched.
static int post(CFStringRef source, CFStringRef type, CFTypeRef value) {
    IOHIDEventSystemClientRef client =
        IOHIDEventSystemClientCreateWithType(kCFAllocatorDefault, kEventSystemClientSimple, NULL);
    if (client == NULL) {
        fprintf(stderr, "duo-pose-helper: no HID event system client\n");
        return 1;
    }
    const void *keys[] = {CFSTR("provider"), CFSTR("source"), CFSTR("type"), CFSTR("value")};
    const void *values[] = {CFSTR("com.apple.Virtualization"), source, type, value};
    CFDictionaryRef payload =
        CFDictionaryCreate(kCFAllocatorDefault, keys, values, 4, &kCFTypeDictionaryKeyCallBacks,
                           &kCFTypeDictionaryValueCallBacks);
    int status = 1;
    CFDataRef data = IOCFSerialize(payload, kSerializeBinary);
    if (data == NULL) {
        fprintf(stderr, "duo-pose-helper: the payload did not serialize\n");
    } else {
        IOHIDEventRef event = IOHIDEventCreateVendorDefinedEvent(
            kCFAllocatorDefault, mach_absolute_time(), kDeviceStateUsagePage, kDeviceStateUsage, 0,
            CFDataGetBytePtr(data), CFDataGetLength(data), 0);
        if (event == NULL) {
            fprintf(stderr, "duo-pose-helper: no vendor-defined event\n");
        } else {
            IOHIDEventSystemClientDispatchEvent(client, event);
            CFRelease(event);
            status = 0;
        }
        CFRelease(data);
    }
    CFRelease(payload);
    // Delivery is asynchronous: the client stays a moment so the event leaves this process.
    usleep(100000);
    CFRelease(client);
    return status;
}

int main(int argc, char **argv) {
    if (argc != 3) return usage();
    if (strcmp(argv[1], "hinge") == 0) {
        char *end = NULL;
        double degrees = strtod(argv[2], &end);
        // Written so that "nan" fails it too.
        if (end == argv[2] || *end != '\0' || !(degrees >= 0 && degrees <= 180)) return usage();
        CFNumberRef number = CFNumberCreate(kCFAllocatorDefault, kCFNumberDoubleType, &degrees);
        int status = post(CFSTR("hinge-slider-control"), CFSTR("range"), number);
        CFRelease(number);
        return status;
    }
    if (strcmp(argv[1], "orientation") == 0) {
        for (size_t i = 0; i < sizeof kOrientations / sizeof kOrientations[0]; i++) {
            if (strcmp(argv[2], kOrientations[i]) != 0) continue;
            CFStringRef name =
                CFStringCreateWithCString(kCFAllocatorDefault, argv[2], kCFStringEncodingUTF8);
            int status = post(CFSTR("orientation-picker-control"), CFSTR("enum"), name);
            CFRelease(name);
            return status;
        }
    }
    return usage();
}
