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
{{- $u := required "nova.url must be set to the Nova Compute endpoint from your Keystone catalog (e.g. --set nova.url=https://compute.gra11.cloud.ovh.net/v2.1/<projectId>)" .Values.nova.url -}}
{{/*
WHY THE SHAPE IS CHECKED HERE AND NOT LEFT TO THE FIRST REQUEST.

A nova.url missing its `/v2.1/<projectId>` suffix 404s every create. That used
to cost a confusing error; since oasgen#818 it costs a STUCK RESOURCE: a create
answering 404 stamps `external-create-pending` and never clears it, because 404
is excluded from the classification that would clear the stamp. On this chart it
compounds -- no kind here declares a `findby`, so the existence check cannot
answer either and the CR has no road back. `kubectl delete` may then hang too,
because the recovery gate runs before the deletion dispatch.

So a typo at install time surfaces hours later as an unremovable object. Refusing
it here turns that into one line at `helm install`, before anything exists to get
stuck. This is the entry docs/e2e.md's troubleshooting list describes having
actually happened.

WHAT THIS CATCHES AND WHAT IT CANNOT, stated so nobody reads it as more:
  CATCHES   a missing, empty or malformed `/v2.1/<segment>` path
  CANNOT    a wrong but well-formed project id. That URL is valid and only a
            real request can find it out, which is why the troubleshooting entry
            stays where it is rather than being replaced by this.

Both documented real-world forms carry the suffix (values.yaml names the OVH and
the in-cluster ClusterIP shapes), so nothing legitimate is rejected.
*/}}
{{- if not (regexMatch "/v2\\.1/[^/[:space:]]+" $u) -}}
{{- fail (printf "nova.url must include a /v2.1/<projectId> path and yours does not: %s\n\nA Nova endpoint without the project segment 404s every create, and since\noasgen-provider#818 a 404'd create latches `external-create-pending` on the CR\nand never clears it -- on this chart, which declares no findby, such a CR may\nalso refuse to delete. This is refused at install rather than discovered later.\n\n  OVH         https://compute.<region>.cloud.ovh.net/v2.1/<projectId>\n  in-cluster  http://<nova-api ClusterIP>:8774/v2.1/<projectId>\n\nRead it from your Keystone service catalog, or with ./scripts/get-token.sh --upstream.\nNOTE this check cannot tell a WRONG project id from a right one -- it only\nrejects a missing or malformed path." $u) -}}
{{- end -}}
{{- $u -}}
{{- end -}}
