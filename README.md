# Wizard snack animatronic — Pi-side ROS 2 workspace

## Dev environment (Mac)

All development and testing happens in a Docker container running ROS 2 Jazzy
(`ros:jazzy-ros-base`, native arm64 image), so nothing needs to be installed
on the Mac itself besides Docker.

```sh
docker compose build dev          # one-time (or after editing docker/Dockerfile)
docker compose run --rm dev bash  # opens a shell in /workspace/animatronic_ws
```

Inside the container:

```sh
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install
source install/setup.bash
```

The repo root is bind-mounted at `/workspace`, so edits made on the Mac are
immediately visible in the container and vice versa. `build/`, `install/`,
and `log/` under `animatronic_ws/` are colcon artifacts and are gitignored —
safe to delete any time.

This container is for Mac-side development only. Deployment to the Raspberry
Pi uses native ROS 2 Jazzy packages (not this Docker image) — see Step 0 in
`CLAUDE.md`.
