# Android APK builder — JDK + Android SDK + Gradle, used ONLY by android/build.sh
# (docker run, never a compose service: prod would otherwise pull a ~5 GB toolchain
# it never needs — the APK is built on dev and shipped as an artifact by deploy).
FROM eclipse-temurin:21-jdk-noble

ENV ANDROID_HOME=/opt/android-sdk \
    PATH=/opt/gradle/bin:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:$PATH

COPY docker/apt-sources.sh /usr/local/bin/apt-sources
RUN apt-sources && apt-get update && apt-get install -y --no-install-recommends unzip wget git \
    && rm -rf /var/lib/apt/lists/*

# Gradle pinned explicitly (no wrapper: the wrapper would re-download Gradle into
# every fresh container; baking it in keeps builds hermetic + offline-ish).
ARG GRADLE_VERSION=9.7.1
RUN wget -q "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" -O /tmp/gradle.zip \
    && unzip -q /tmp/gradle.zip -d /opt && mv "/opt/gradle-${GRADLE_VERSION}" /opt/gradle \
    && rm /tmp/gradle.zip

# Android command-line tools + the SDK pieces the app build needs. The build runs
# as the invoking user and cannot add to the SDK, so a component AGP asks for
# fails the build until it is listed here.
ARG CMDLINE_TOOLS=16111833
RUN mkdir -p "$ANDROID_HOME/cmdline-tools" \
    && wget -q "https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS}_latest.zip" -O /tmp/ct.zip \
    && unzip -q /tmp/ct.zip -d "$ANDROID_HOME/cmdline-tools" \
    && mv "$ANDROID_HOME/cmdline-tools/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest" \
    && rm /tmp/ct.zip \
    && yes | sdkmanager --licenses >/dev/null \
    && sdkmanager "platform-tools" "platforms;android-37.2" "build-tools;37.0.0" >/dev/null \
    && chmod -R a+rX "$ANDROID_HOME"

WORKDIR /project
