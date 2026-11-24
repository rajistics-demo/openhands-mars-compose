#!/usr/bin/env python3
"""Helm post-renderer: mount the MARS bootstrap secrets into the official
openhands chart's own app Deployment.

This exists for exactly one, evidenced gap: the official chart's
``openhands`` Deployment (templates/deployment.yaml in the 0.119.0 chart)
has no ``extraVolumes``/``extraVolumeMounts`` value - only ``env`` (plain
string overrides; confirmed by reading templates/_env.yaml's
``openhands.env`` helper, which renders every ``.Values.env`` entry as
``{name, value}`` and cannot express a ``valueFrom.secretKeyRef`` or a
volume mount). ``DO_MARS_TEAM_TOKEN_FILE`` and friends are themselves
plain file-path strings and are set through that ``env`` map in
../values/openhands-mars.values.yaml - only the volume carrying the file
at that path needs this script.

This follows Helm's documented post-rendering protocol
(https://helm.sh/docs/topics/advanced/#post-rendering): read the fully
rendered multi-document YAML manifest stream on stdin, write the modified
stream to stdout. Everything except the one targeted Deployment passes
through unchanged. Install with:

    helm template openhands oci://ghcr.io/openhands/helm-charts/openhands \\
      --version 0.119.0 -f values/openhands-mars.values.yaml \\
      --post-renderer doks/helm/postrender/mount_mars_secrets.py

(or ``helm install``/``helm upgrade`` with the same ``--post-renderer``
flag). See ../../docs/HELM.md for the full command and
MARS_SECRET_NAME/MARS_POSTRENDER_DEPLOYMENT for the two env vars this
script reads.

A YAML round-trip (``yaml.safe_load_all``/``safe_dump_all``) can reformat
comments/quote style on every document it passes through, same documented
tradeoff the official chart's own ``openhands.env`` helper already makes
for its ``fromYamlArray``/``toYaml`` round-trip (templates/_env.yaml) -
cosmetic only, not a behavior change.
"""

import copy
import os
import sys

import yaml

DEPLOYMENT_NAME = os.environ.get('MARS_POSTRENDER_DEPLOYMENT', 'openhands')
SECRET_NAME = os.environ.get('MARS_SECRET_NAME')
CONTAINER_NAME = os.environ.get('MARS_POSTRENDER_CONTAINER', 'openhands')

VOLUME = {
    'name': 'mars-secrets',
    'secret': {
        'secretName': None,  # filled in below once SECRET_NAME is known
        'defaultMode': 0o440,
        'items': [
            {'key': 'mars_team_token', 'path': 'mars_team_token'},
            {'key': 'mars_session_signing_key', 'path': 'mars_session_signing_key'},
            {'key': 'mars_init_api_key', 'path': 'mars_init_api_key'},
        ],
    },
}
VOLUME_MOUNT = {
    'name': 'mars-secrets',
    'mountPath': '/run/secrets',
    'readOnly': True,
}


def patch_deployment(doc: dict) -> dict:
    spec = doc.setdefault('spec', {}).setdefault('template', {}).setdefault(
        'spec', {}
    )
    # A rendered key can be present with a null value (e.g. an empty
    # `volumes:`/`volumeMounts:` block left by a conditional that matched
    # nothing), not just absent - setdefault only fills in an absent key,
    # so normalize null-valued keys to [] first in both places below.
    volumes = spec.get('volumes') or []
    if not any(v.get('name') == 'mars-secrets' for v in volumes):
        volume = copy.deepcopy(VOLUME)
        volume['secret']['secretName'] = SECRET_NAME
        volumes.append(volume)
    spec['volumes'] = volumes

    containers = spec.get('containers') or []
    target = next((c for c in containers if c.get('name') == CONTAINER_NAME), None)
    if target is None:
        raise SystemExit(
            f'mount_mars_secrets: no container named {CONTAINER_NAME!r} in '
            f'Deployment/{DEPLOYMENT_NAME} - the official chart may have '
            'renamed its container; update MARS_POSTRENDER_CONTAINER or '
            'this script before trusting this patch.'
        )
    mounts = target.get('volumeMounts') or []
    if not any(m.get('name') == 'mars-secrets' for m in mounts):
        mounts.append(dict(VOLUME_MOUNT))
    target['volumeMounts'] = mounts
    return doc


def main() -> None:
    if not SECRET_NAME:
        raise SystemExit(
            'mount_mars_secrets: MARS_SECRET_NAME env var is required (the '
            'existing Secret with keys mars_team_token/'
            'mars_session_signing_key/mars_init_api_key - see '
            'doks/docs/HELM.md).'
        )
    raw = sys.stdin.read()
    docs = list(yaml.safe_load_all(raw))
    patched_count = 0
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if (
            doc.get('kind') == 'Deployment'
            and doc.get('metadata', {}).get('name') == DEPLOYMENT_NAME
        ):
            patch_deployment(doc)
            patched_count += 1
    if patched_count != 1:
        raise SystemExit(
            f'mount_mars_secrets: expected exactly one Deployment named '
            f'{DEPLOYMENT_NAME!r} in the rendered manifest stream, found '
            f'{patched_count}. Refusing to emit output rather than silently '
            'mounting the secret nowhere (patched_count=0) or into more than '
            'one Deployment (patched_count>1).'
        )
    sys.stdout.write(yaml.safe_dump_all(docs, default_flow_style=False, sort_keys=False))


if __name__ == '__main__':
    main()
