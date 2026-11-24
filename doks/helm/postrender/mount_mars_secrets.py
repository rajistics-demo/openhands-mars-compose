#!/usr/bin/env python3
"""Mount MARS credentials for official Helm workloads configured to use them."""

import os
import sys

import yaml

MOUNT = {'name': 'mars-secrets', 'mountPath': '/run/mars-secrets', 'readOnly': True}
KEYS = ('mars_team_token', 'mars_session_signing_key', 'mars_init_api_key')
TEAM_TOKEN_ENV = 'DO_MARS_TEAM_TOKEN_FILE'
PROVIDER_ENV = {
    'OH_SANDBOX_KIND': 'openhands.app_server.sandbox.do_mars_sandbox_service.DoMarsSandboxServiceInjector',
    'OH_SANDBOX_SPEC_KIND': 'openhands.app_server.sandbox.do_mars_spec_service.DoMarsSpecServiceInjector',
}


def pod_spec(doc: dict) -> dict | None:
    spec = doc.get('spec') or {}
    if doc.get('kind') == 'CronJob':
        spec = (spec.get('jobTemplate') or {}).get('spec') or {}
    elif doc.get('kind') not in {'Deployment', 'Job'}:
        return None
    return (spec.get('template') or {}).get('spec')


def needs_credentials(container: dict, app_image: str) -> bool:
    return container.get('image') == app_image and any(
        setting.get('name') == TEAM_TOKEN_ENV
        and setting.get('value') == MOUNT['mountPath'] + '/' + KEYS[0]
        for setting in (container.get('env') or [])
    )


def add_named(target: dict, key: str, item: dict) -> None:
    items = target.get(key) or []
    existing = [entry for entry in items if entry.get('name') == item['name']]
    if existing and existing != [item]:
        raise SystemExit(f'mount_mars_secrets: conflicting {key} entry for {item["name"]}')
    if not existing:
        items.append(item)
    target[key] = items


def main() -> None:
    secret_name = os.environ.get('MARS_SECRET_NAME')
    if not secret_name:
        raise SystemExit('mount_mars_secrets: set MARS_SECRET_NAME to the existing Kubernetes Secret.')
    deployment_name = os.environ.get('MARS_POSTRENDER_DEPLOYMENT', 'openhands')
    container_name = os.environ.get('MARS_POSTRENDER_CONTAINER', 'openhands')
    docs = list(yaml.safe_load_all(sys.stdin))
    apps = [
        pod_spec(doc) for doc in docs
        if isinstance(doc, dict) and doc.get('kind') == 'Deployment'
        and doc.get('metadata', {}).get('name') == deployment_name
    ]
    if len(apps) != 1 or not apps[0]:
        raise SystemExit(f'mount_mars_secrets: expected one Deployment/{deployment_name}.')
    containers = [c for c in apps[0].get('containers', []) if c.get('name') == container_name]
    if len(containers) != 1 or not needs_credentials(containers[0], containers[0]['image']):
        raise SystemExit(f'mount_mars_secrets: expected MARS file configuration for {container_name}.')
    app_image = containers[0]['image']
    volume = {
        'name': MOUNT['name'],
        'secret': {
            'secretName': secret_name,
            'defaultMode': 0o440,
            'items': [{'key': key, 'path': key} for key in KEYS],
        },
    }
    for doc in docs:
        if (
            not isinstance(doc, dict)
            or 'helm.sh/hook' in (doc.get('metadata', {}).get('annotations') or {})
            or not (spec := pod_spec(doc))
        ):
            continue
        matching = [
            c for group in ('containers', 'initContainers') for c in (spec.get(group) or [])
            if needs_credentials(c, app_image)
        ]
        if matching:
            add_named(spec, 'volumes', volume)
            for container in matching:
                add_named(container, 'volumeMounts', MOUNT)
                container['env'] = [
                    entry for entry in container['env'] if entry['name'] not in PROVIDER_ENV
                ] + [{'name': name, 'value': value} for name, value in PROVIDER_ENV.items()]
    yaml.safe_dump_all(docs, sys.stdout, sort_keys=False)


if __name__ == '__main__':
    main()
