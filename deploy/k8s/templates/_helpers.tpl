{{/*
Common helpers — image refs, labels, env vars.
*/}}
{{- define "vg.image" -}}
{{- $img := index .Values.image .component -}}
{{- $reg := .Values.image.registry -}}
{{- if $reg -}}{{ $reg }}/{{ end }}{{ $img.repository }}:{{ $img.tag }}
{{- end -}}

{{- define "vg.labels" -}}
app.kubernetes.io/name: vivoguard
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "vg.matchLabels" -}}
app.kubernetes.io/name: vivoguard
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The set of env vars every app pod (api / worker / streamer) needs.
Pulled from the Secret + values.
*/}}
{{- define "vg.appEnv" -}}
- { name: APP_ENV, value: "production" }
- { name: JWT_SECRET, valueFrom: { secretKeyRef: { name: vivoguard-secrets, key: jwt-secret } } }
- { name: CREDENTIALS_FERNET_KEY, valueFrom: { secretKeyRef: { name: vivoguard-secrets, key: fernet-key } } }
- { name: BOOTSTRAP_ADMIN_EMAIL,    value: {{ .Values.secrets.bootstrapAdminEmail | quote }} }
- { name: BOOTSTRAP_ADMIN_PASSWORD, valueFrom: { secretKeyRef: { name: vivoguard-secrets, key: bootstrap-admin-pw } } }
{{- if .Values.postgres.enabled }}
- { name: POSTGRES_HOST,     value: "vivoguard-postgres" }
- { name: POSTGRES_PORT,     value: "5432" }
- { name: POSTGRES_DB,       value: "vivoguard" }
- { name: POSTGRES_USER,     value: "vivoguard" }
- { name: POSTGRES_PASSWORD, valueFrom: { secretKeyRef: { name: vivoguard-secrets, key: postgres-password } } }
{{- else if .Values.externalDatabase.url }}
- { name: DATABASE_URL, value: {{ .Values.externalDatabase.url | quote }} }
{{- end }}
{{- if .Values.redis.enabled }}
- { name: REDIS_HOST, value: "vivoguard-redis" }
- { name: REDIS_PORT, value: "6379" }
{{- else if .Values.externalRedis.url }}
- { name: REDIS_URL, value: {{ .Values.externalRedis.url | quote }} }
{{- end }}
{{- if .Values.minio.enabled }}
- { name: S3_ENDPOINT,   value: "http://vivoguard-minio:9000" }
- { name: S3_ACCESS_KEY, value: "minioadmin" }
- { name: S3_SECRET_KEY, valueFrom: { secretKeyRef: { name: vivoguard-secrets, key: s3-secret-key } } }
- { name: S3_USE_SSL,    value: "false" }
{{- else if .Values.externalS3.endpoint }}
- { name: S3_ENDPOINT,   value: {{ .Values.externalS3.endpoint | quote }} }
- { name: S3_ACCESS_KEY, value: {{ .Values.externalS3.accessKey | quote }} }
- { name: S3_SECRET_KEY, valueFrom: { secretKeyRef: { name: vivoguard-secrets, key: s3-secret-key } } }
- { name: S3_USE_SSL,    value: {{ .Values.externalS3.useSSL | quote }} }
{{- end }}
- { name: USE_GPU,        value: {{ .Values.worker.gpu.enabled | quote }} }
- { name: DEFAULT_MODEL,  value: {{ .Values.inference.defaultModel | quote }} }
{{- with .Values.notifications.smtp }}
- { name: SMTP_HOST,     value: {{ .host | quote }} }
- { name: SMTP_PORT,     value: {{ .port | quote }} }
- { name: SMTP_USER,     value: {{ .user | quote }} }
- { name: SMTP_PASSWORD, valueFrom: { secretKeyRef: { name: vivoguard-secrets, key: smtp-password, optional: true } } }
- { name: SMTP_FROM,     value: {{ .from | quote }} }
- { name: SMTP_USE_TLS,  value: {{ .tls | quote }} }
{{- end }}
{{- with .Values.notifications.twilio }}
- { name: TWILIO_ACCOUNT_SID, value: {{ .sid | quote }} }
- { name: TWILIO_AUTH_TOKEN,  valueFrom: { secretKeyRef: { name: vivoguard-secrets, key: twilio-token, optional: true } } }
- { name: TWILIO_FROM_NUMBER, value: {{ .fromNumber | quote }} }
{{- end }}
{{- with .Values.notifications.webhook }}
- { name: WEBHOOK_URL,         value: {{ .url | quote }} }
- { name: WEBHOOK_AUTH_HEADER, value: {{ .authHeader | quote }} }
{{- end }}
{{- end -}}
