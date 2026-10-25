# ROS 2 Jazzy + Gazebo Harmonic (arm64)。osrf/ros の desktop-full は amd64 のみなので ros-base から積む。
# ホストには何も入れず、シミュレーション・制御・動画の合成はすべてこの中で動かす(LLM だけはホストのサーバを使う)。
FROM --platform=linux/arm64 ros:jazzy-ros-base

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-jazzy-ros-gz \
        ros-jazzy-gz-ros2-control \
        ros-jazzy-ros2-control \
        ros-jazzy-ros2-controllers \
        ros-jazzy-xacro \
        ros-jazzy-ur-description \
        ros-jazzy-ur-simulation-gz \
        python3-pip python3-jsonschema python3-numpy python3-yaml python3-pytest python3-matplotlib ffmpeg \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 画像処理・ローカルLLMクライアント。opencv は numpy 1.x(ROS の apt 版)と共存できる版に固定する。
RUN pip3 install --no-cache-dir --break-system-packages \
        "opencv-contrib-python-headless==4.10.0.84" \
        openai

WORKDIR /ws
SHELL ["/bin/bash", "-c"]
RUN echo 'source /opt/ros/jazzy/setup.bash' >> /root/.bashrc
