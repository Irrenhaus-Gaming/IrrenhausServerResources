# Real Minecraft Java client for visual checks against the local `mc play` server.
# It runs on a virtual X screen (Xvfb) with Mesa software rendering (llvmpipe / lavapipe), so it
# needs no GPU and never touches the host's display. `mc client …` drives it with xdotool and takes
# screenshots of that virtual screen only. The game itself (client jar, assets, Mojang's JVM) is
# downloaded by portablemc into the volume mounted at /data on first start.
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb xdotool x11-utils imagemagick \
        libgl1 libgl1-mesa-dri libglx-mesa0 libegl1 libgles2 mesa-vulkan-drivers libvulkan1 \
        libxrandr2 libxcursor1 libxi6 libxxf86vm1 libxtst6 libxinerama1 libxext6 libxrender1 \
        libasound2 libpulse0 libopenal1 fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir portablemc==5.0.6
COPY entrypoint.sh /usr/local/bin/mc-client-entrypoint
RUN chmod 0755 /usr/local/bin/mc-client-entrypoint
ENV DISPLAY=:99 PMC_MAIN_DIR=/data LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe
ENTRYPOINT ["/usr/local/bin/mc-client-entrypoint"]
