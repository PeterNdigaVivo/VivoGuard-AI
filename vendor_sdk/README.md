# vendor_sdk/

This directory is an **optional** drop-in slot for proprietary vendor C SDKs
(Dahua NetSDK, Hikvision HCNetSDK).

The default VivoGuard build does **not** depend on these SDKs — all camera
and NVR integration is via documented HTTP, RTSP, ISAPI, and ONVIF
interfaces.

If a future plugin adds ctypes wrappers, place the vendor `.so` files here:

```
vendor_sdk/
  dahua/libdhnetsdk.so
  hikvision/libhcnetsdk.so
```

These files are **gitignored** and must never be committed.
