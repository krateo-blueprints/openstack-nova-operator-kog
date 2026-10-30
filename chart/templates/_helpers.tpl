{{/*
Expand the name of the chart.
*/}}
{{- define "openstack-nova-operator-kog.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "openstack-nova-operator-kog.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart label.
*/}}
{{- define "openstack-nova-operator-kog.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "openstack-nova-operator-kog.labels" -}}
helm.sh/chart: {{ include "openstack-nova-operator-kog.chart" . }}
{{ include "openstack-nova-operator-kog.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "openstack-nova-operator-kog.selectorLabels" -}}
app.kubernetes.io/name: {{ include "openstack-nova-operator-kog.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
The Nova Compute endpoint the generated controller calls directly.

Taken from the Keystone service catalog for your project and region, e.g.
  https://compute.gra11.cloud.ovh.net/v2.1/<projectId>   (OVH Public Cloud)
  http://<nova-api ClusterIP>:8774/v2.1/<projectId>      (in-cluster Nova)

Use an IP literal for an in-cluster Nova: a *.svc.cluster.local name is
resolved by the controller's own resolver, not by nginx, so the historic
ClusterIP caveat no longer applies - but an IP remains the safest default.
*/}}
{{- define "openstack-nova-operator-kog.novaUrl" -}}
{{- required "nova.url must be set to the Nova Compute endpoint from your Keystone catalog (e.g. --set nova.url=https://compute.gra11.cloud.ovh.net/v2.1/<projectId>)" .Values.nova.url -}}
{{- end -}}
