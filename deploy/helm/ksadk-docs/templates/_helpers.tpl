{{- define "ksadk-docs.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ksadk-docs.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- include "ksadk-docs.name" . -}}
{{- end -}}
{{- end -}}

{{- define "ksadk-docs.labels" -}}
app.kubernetes.io/name: {{ include "ksadk-docs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}

{{- define "ksadk-docs.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ksadk-docs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
