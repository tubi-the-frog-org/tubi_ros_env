cmake_minimum_required(VERSION 3.8)
project(animatronic_interfaces)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/Response.msg"
  "msg/AudioReady.msg"
  "msg/EspStatus.msg"
)

ament_package()
