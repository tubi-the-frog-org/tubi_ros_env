FROM ros:jazzy-ros-base

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-colcon-common-extensions \
    python3-pip \
    python3-pytest \
    build-essential \
    git \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages \
    aiohttp \
    pyserial \
    requests

WORKDIR /workspace/animatronic_ws

RUN echo "source /opt/ros/jazzy/setup.bash" >> /root/.bashrc && \
    echo "[ -f /workspace/animatronic_ws/install/setup.bash ] && source /workspace/animatronic_ws/install/setup.bash" >> /root/.bashrc

CMD ["bash"]
