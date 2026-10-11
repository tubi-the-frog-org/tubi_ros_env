idf_component_register(SRCS "main.c" "servos.c" "link_demux.c" "mp3_pipe.c"
                       INCLUDE_DIRS "." "../include"
                       PRIV_REQUIRES driver log freertos esp_system)

# minimp3 is third-party code; don't let its warnings fail the build
set_source_files_properties(${CMAKE_CURRENT_SOURCE_DIR}/mp3_pipe.c
                            PROPERTIES COMPILE_FLAGS "-Wno-error")
