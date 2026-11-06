# Device log lines for the notification watch

SpringBoard's lines as `idevicesyslog -u UDID --no-colors -p SpringBoard` printed them from the
owner's iPhone (iOS 27.2) over USB on 2026-11-05, for `device_notifications.presentations`
(vocaro-engineering#144). They are recordings, kept to the lines about two test notifications, and
they hold identifiers only: no app, person or message of the owner's.

- `springboard-banner.log`: Shortcuts' test notification `8E7B-02D4`, shown as a banner. It is
  posted to `CSNotificationDispatcher` and `SBNotificationBannerDestination`, will appear, and
  did appear at 17:03:49 by the device's clock. Only the last line is a banner shown.
- `springboard-quiet.log`: a "Vocaro test" reminder, `B255-16C4`, posted from the Reminders app in
  the background while the owner's Do Not Disturb was on. It is posted to the banner destination
  too, then dropped: `suppressed: 1 … do not disturb: 1`. None of these is a banner shown.

Replace or extend them only with lines recorded the same way, from a test notification that says
nothing of the owner's (guides/device-testing.md, Verifying on a device).
