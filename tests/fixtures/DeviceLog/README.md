# Device log lines for the notification watch

SpringBoard's lines as `idevicesyslog --no-colors -p SpringBoard` prints them, for
`device_notifications.presentations` (vocaro-engineering#144). They are **constructed** in that
format and are not recordings: no device has yet been watched while a notification arrived, and
a recording of the owner's devices would carry his notifications. Every message here is made up,
and the parts iOS keeps private are shown as `<private>`.

- `springboard-banner.log`: two banners presented, at 00:52:13 and 00:52:40 by the device's
  clock, among SpringBoard's ordinary lines.
- `springboard-quiet.log`: SpringBoard's ordinary lines, a notification that Do Not Disturb
  held back from a banner, and another process naming the banner destination. None of these
  is a banner presented.

Replace them with SpringBoard's lines from the first window that verifies #144, in which a test
notification that says nothing of the owner's is shown with Do Not Disturb off and then on
(guides/device-testing.md, Notifications during a run). Keep only lines that name no app,
person or message of the owner's.
