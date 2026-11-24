{{/*
Chart name used as a prefix for every resource this chart renders.
*/}}
{{- define "mars-doks.fullname" -}}
{{- printf "%s-mars" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mars-doks.labels" -}}
app.kubernetes.io/name: mars-doks
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/*
Host part of a required absolute https:// URL value. Used so the Ingress
host for webUrl/gatewayUrl can never diverge from the value actually fed
into --lock-to-cloud / DO_MARS_GATEWAY_URL - one required input, two
derived uses, no second place for an operator to keep in sync by hand.
*/}}
{{- define "mars-doks.hostFromUrl" -}}
{{- $url := required "a URL is required" . -}}
{{- if not (regexMatch "^https://[^/]+/?$" $url) -}}
{{- fail (printf "%q must be an https:// origin with no path (e.g. https://app.example.com)" $url) -}}
{{- end -}}
{{- $withoutScheme := trimPrefix "https://" $url -}}
{{- trimSuffix "/" $withoutScheme -}}
{{- end -}}

{{/*
Shared DB env vars for mars-pool-reconciler, the only workload this
companion chart itself runs that talks to the Enterprise database
directly. (app/worker/automation/canvas are the official openhands chart's
own Deployments now - see ../../../docs/HELM.md - and get the matching
DB_* vars from *that* chart's own values, not from here.) mars-gateway has
its own variant below so it can use a separate, restricted-role Secret.
*/}}
{{- define "mars-doks.dbEnv" -}}
- name: DB_HOST
  value: {{ required "database.host is required" .Values.database.host | quote }}
- name: DB_PORT
  value: {{ .Values.database.port | default 5432 | quote }}
- name: DB_NAME
  value: {{ .Values.database.name | default "openhands" | quote }}
- name: DB_USER
  valueFrom:
    secretKeyRef:
      name: {{ required "database.existingSecret is required" .Values.database.existingSecret }}
      key: DB_USER
- name: DB_PASS
  valueFrom:
    secretKeyRef:
      name: {{ required "database.existingSecret is required" .Values.database.existingSecret }}
      key: DB_PASS
{{- end -}}

{{- define "mars-doks.gatewayDbEnv" -}}
{{- $secret := .Values.database.gatewayExistingSecret | default .Values.database.existingSecret -}}
- name: DB_HOST
  value: {{ required "database.host is required" .Values.database.host | quote }}
- name: DB_PORT
  value: {{ .Values.database.port | default 5432 | quote }}
- name: DB_NAME
  value: {{ .Values.database.name | default "openhands" | quote }}
- name: DB_USER
  valueFrom:
    secretKeyRef:
      name: {{ required "database.existingSecret or database.gatewayExistingSecret is required" $secret }}
      key: DB_USER
- name: DB_PASS
  valueFrom:
    secretKeyRef:
      name: {{ required "database.existingSecret or database.gatewayExistingSecret is required" $secret }}
      key: DB_PASS
{{- end -}}

{{/*
Pool env vars for mars-pool-reconciler (drives the reconcile loop itself).
The official openhands Deployment's app/worker processes read the exact
same DO_MARS_POOL_* names at injector-construction time to decide whether
to attempt claim_one - set there via the official chart's `env` map (see
../../../docs/HELM.md), not via this template, since this chart no longer
renders that Deployment. Always rendered here - disabled by default via
pool.enabled - so a stock, pool-less install and a pool install differ
only in this block's values, never in which env vars exist.
*/}}
{{- define "mars-doks.poolEnv" -}}
- name: DO_MARS_POOL_ENABLED
  value: {{ .Values.doMars.pool.enabled | quote }}
- name: DO_MARS_POOL_SANDBOX_SPEC_ID
  value: {{ .Values.doMars.pool.sandboxSpecId | quote }}
- name: DO_MARS_POOL_MIN_HOT
  value: {{ .Values.doMars.pool.minHot | default 0 | quote }}
- name: DO_MARS_POOL_MIN_PAUSED_RESERVE
  value: {{ .Values.doMars.pool.minPausedReserve | default 0 | quote }}
- name: DO_MARS_POOL_MAX_TOTAL
  value: {{ .Values.doMars.pool.maxTotal | default 0 | quote }}
{{- end -}}

{{/*
Mount for the three bootstrap secrets (do_team_token, session_signing_key,
init_api_key - same names/contract as the Compose package's `secrets:`
block). The existingSecret must carry keys named exactly
mars_team_token / mars_session_signing_key / mars_init_api_key.
*/}}
{{- define "mars-doks.secretVolume" -}}
- name: mars-secrets
  secret:
    secretName: {{ required "secrets.existingSecret is required" .Values.secrets.existingSecret }}
    defaultMode: 0440
    items:
      - key: mars_team_token
        path: mars_team_token
      - key: mars_session_signing_key
        path: mars_session_signing_key
      - key: mars_init_api_key
        path: mars_init_api_key
{{- end -}}

{{- define "mars-doks.secretVolumeMount" -}}
- name: mars-secrets
  mountPath: /run/mars-secrets
  readOnly: true
{{- end -}}
