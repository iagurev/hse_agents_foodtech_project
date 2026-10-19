---
name: docker_deployment
description: Defines Docker conventions using the actual repository services, ports, and runtime configuration.
---

# Docker Standards

These conventions apply when an application uses Docker. The Harness does not require a container runtime or define a deployment topology. Follow the actual Dockerfile, Compose configuration, and documented commands once they exist. Do not infer service names, ports, APIs, or topology from examples in the Harness.

## Compose Runtime

When the repository has a Compose stack, validate its configuration without printing resolved secrets, inspect the current services, and reuse healthy instances where possible:

```bash
docker compose config --quiet
docker compose ps
```

When services need to be built or started, use the documented stack. For long-running services that will be exercised by requests or other checks, use detached mode:

```bash
docker compose up -d --build
```

Derive required services and ports from the actual configuration. Check readiness using the project's defined health or smoke checks. Do not launch extra local servers or alternate-port containers when the existing stack is sufficient.

One-off jobs and test runs should exit when finished; detached mode is not required for them. A pattern such as `docker compose run --rm <service> <command>` applies only to a service and command that actually exist.

## Build And Environment

- Use the documented build context and descriptive image tags. A standalone pattern is `docker build -t <image-name> <build-context>`.
- Keep secrets out of images, Docker build arguments, and committed configuration. Provide runtime settings through configured Compose environment fields, `env_file`, or another documented runtime mechanism.
- Use `--env-file` only when an actual file is needed and available; do not require `.env` for every container or verification command.
- Preserve the image's intended entrypoint and command unless the task requires an explicit override.
- Let Compose scope resource names by project, or choose an explicit unique name for standalone temporary containers. Directory basenames alone do not guarantee uniqueness.

## Verification And Cleanup

Pure unit tests, lint, and type checks may run locally through `uv`; Docker is used for container builds, integration checks, and runtime verification when applicable. If Docker configuration is absent, report that verification as unavailable rather than creating an unrelated stack.

If isolation or a networking investigation requires an extra process or container, explain why and remove the temporary resources you created before finishing. Preserve pre-existing services, persistent volumes, and user data unless their removal is part of the authorized task.
