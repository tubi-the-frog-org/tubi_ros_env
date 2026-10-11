# This file was automatically generated for projects
# without default 'CMakeLists.txt' file.

FILE(GLOB_RECURSE app_sources ${CMAKE_SOURCE_DIR}/src/*.*)

idf_component_register(SRCS "main.c" "mp3_pipe.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES driver log freertos )
 
# minimp3 is third-party code; don't let its warnings fail the build
set_source_files_properties(${CMAKE_CURRENT_SOURCE_DIR}/mp3_pipe.c
                            PROPERTIES COMPILE_FLAGS "-Wno-error")