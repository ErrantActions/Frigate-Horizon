---
id: native_notifications
title: Native notification confidence and photos
---

# Native notification confidence and photos

Native activity notifications can use a separate confidence policy without
changing detection filters, recording quality, or browser notifications:

```yaml
apns:
  minimum_confidence: 0.85
  require_coral_confirmation: true
  image_notifications: true
```

The cutoff uses the stored tracked object's highest rolling-median confidence,
not a single-frame score or a camera alarm's claimed confidence. A review is
eligible when at least one linked, same-camera Coral object meets the cutoff.
Unscored camera-native alarms, unsupported analysis sources, and alerts without
matching evidence do not qualify. Review alerts wait for completion when Coral
confirmation is required. This policy does not enable additional decoding or
analysis, so camera-native vehicle or animal alarms without Coral confirmation
will not produce native activity notifications. Tests and health alerts remain
independent. Defaults preserve existing delivery behavior.

Rejected native-only activity does not consume the shared cooldown. When
browser delivery is also configured, its existing shared cooldown is unchanged.
Eligibility is rechecked when queued notifications are delivered.

Private photos also require the compatible iPhone Notification Service
Extension and authenticated thumbnail fetch. Enabling the server setting alone
cannot add attachments to an older app. Missing or unauthorized images remain
text-only. Restart after changing provider image settings.
