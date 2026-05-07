# Connecting a Dahua NVR over WAN — end-to-end example

Worked example using a remote Dahua NVR exposed via a public IP with port
forwarding (the spec calls out `41.90.110.206`).

## 1. Confirm port forwarding on the router behind the NVR

| Service        | External port | Internal target           |
|----------------|---------------|---------------------------|
| Dahua HTTP     |   80          | NVR LAN IP : 80           |
| Dahua RTSP     |  554          | NVR LAN IP : 554          |
| Dahua TCP/SDK  | 7000          | NVR LAN IP : 37777        |

The "Dahua server" port at the public IP (7000) maps to the LAN-side SDK
port 37777 on the appliance. We do **not** use the C SDK in this build;
the NVR is reached via HTTP/RTSP only. 7000 is shown for reference.

## 2. Validate connectivity from the host running VivoGuard

```bash
# HTTP login page reachable?
curl -I http://41.90.110.206:80/

# RTSP handshake (expect 401 without credentials → confirms port is open)
ffprobe -rtsp_transport tcp \
  rtsp://41.90.110.206:554/cam/realmonitor?channel=1&subtype=0
```

## 3. Add the NVR in VivoGuard

In the UI: **Cameras → Add Camera → "Dahua NVR (HTTP+RTSP)"**.

| Field            | Value                                           |
|------------------|-------------------------------------------------|
| Name             | `Site-A-NVR`                                    |
| Connection type  | `nvr_dahua`                                     |
| Host             | `41.90.110.206`                                 |
| HTTP port        | `80`                                            |
| RTSP port        | `554`                                           |
| Username         | `admin`                                         |
| Password         | (the NVR password)                              |
| Network type     | `WAN`                                           |
| DDNS             | leave blank, or your `*.dyndns.org` hostname    |

Click **Test Connection**. Backend will:

1. `GET http://<host>/cgi-bin/magicBox.cgi?action=getDeviceType` (HTTP digest)
   to verify the device class.
2. `GET .../recordManager.cgi?action=getCaps` to enumerate channel count.
3. Probe `rtsp://.../cam/realmonitor?channel=1&subtype=1` to confirm RTSP.

On success, the backend persists each channel as its own `cameras` row
with the appropriate `channel_number` and a generated RTSP URL.

## 4. Per-channel RTSP templates Dahua uses

```
Main stream  : rtsp://USER:PASS@HOST:554/cam/realmonitor?channel=N&subtype=0
Sub stream   : rtsp://USER:PASS@HOST:554/cam/realmonitor?channel=N&subtype=1
```

The system uses `subtype=1` (lower-resolution substream) for the AI
inference path on WAN cameras to keep bandwidth manageable, and
`subtype=0` for live view and recording.

## 5. Reconnect behaviour

If the WAN link drops, the streamer applies exponential backoff
(`WAN_RECONNECT_INITIAL_DELAY_SECONDS` → `WAN_RECONNECT_MAX_DELAY_SECONDS`
in `.env`) and emits an offline alert if the camera stays down longer
than `WAN_OFFLINE_ALERT_THRESHOLD_SECONDS`.
