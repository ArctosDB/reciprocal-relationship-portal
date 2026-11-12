# Deploy script

This directory contains the Ansible playbook for deploying the HEAD commit of
the service to staging (warbler). To deploy, run:

```sh
cd deploy
uv run ansible-playbook deploy.yml [-e ref=<commit, tag, or branch>]
```

If Ansible does not detect `ansible.cfg`, run:

```sh
export ANSIBLE_CONFIG=./ansible.cfg
```

## Files

|File|Usage|
|---|---|
|[group_vars/staging.yml](group_vars/staging.yml)|Variables used in [deploy.yml](deploy.yml)|
|[templates/env.j2](templates/env.j2)|Environment variables to be used by the service on warbler|
|[ansible.cfg](ansible.cfg)|Ansible config file|
|[deploy.yml](deploy.yml)|Ansible playbook for deploying the service to `warbler`|
|[inventory.yml](inventory.yml)|List of servers Ansible can connect to. Currently only points `staging` to `warbler`|
|[nginx.conf](nginx.conf)|Config for nginx on `warbler` to proxy `/vulpes/` to `warbler:8002`|
|[README.md](README.md)|Documentation for this folder|
