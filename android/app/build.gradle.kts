import java.util.Properties

plugins {
    id("com.android.application")
    id("com.google.gms.google-services")
}

// The deployment host is baked in: it drives the App Link intent filter (the
// setup-QR URL opens the app instead of the browser) and the default WebView
// origin. android/build.sh passes -PdidaHost= from keystore/keystore.properties.
val didaHost: String = (project.findProperty("didaHost") as String?)
    ?: error("didaHost is not set: build through android/build.sh, which takes it from keystore/keystore.properties")
// Version stamped by build.sh from git (same v0.1.<commit-count> scheme as the
// rest of DIDA). The fallback only exists for ad-hoc IDE builds.
val verCode: Int = ((project.findProperty("versionCode") as String?) ?: "1").toInt()
val verName: String = (project.findProperty("versionName") as String?) ?: "v0.0.0-dev"

android {
    namespace = "biz.boskovic.dida"
    compileSdk {
        version = release(37) { minorApiLevel = 2 }
    }
    buildToolsVersion = "37.0.0"

    defaultConfig {
        applicationId = "biz.boskovic.dida"
        minSdk = 26
        targetSdk = 36
        versionCode = verCode
        versionName = verName
        manifestPlaceholders["didaHost"] = didaHost
        buildConfigField("String", "DEFAULT_BASE_URL", "\"https://$didaHost\"")
    }

    lint {
        // targetSdk stays below 37: API 37 makes local-network access a runtime
        // permission, and the deployment host resolves to the LAN at home.
        disable += "OldTargetApi"
    }

    buildFeatures {
        buildConfig = true
    }

    // The language is switched inside the app (attachBaseContext), so every
    // locale has to ship in the one sideloaded APK.
    bundle {
        language {
            enableSplit = false
        }
    }

    signingConfigs {
        create("release") {
            // Generated once by android/build.sh --init-keystore; gitignored and
            // present only on the dev box. Release builds hard-fail without it —
            // an unsigned/debug-signed APK must never reach the family phones
            // (Android would refuse it as an update of the signed install).
            val props = rootProject.file("keystore/keystore.properties")
            if (props.exists()) {
                val p = Properties().apply { props.inputStream().use { load(it) } }
                storeFile = rootProject.file(p.getProperty("storeFile"))
                storePassword = p.getProperty("storePassword")
                keyAlias = p.getProperty("keyAlias")
                keyPassword = p.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    // Only for CrashReporter — the car-pairing UI the module also carries is
    // unreferenced here and R8 strips it.
    implementation(project(":shared"))
    implementation("androidx.core:core-ktx:1.19.0")
    implementation("androidx.activity:activity-ktx:1.13.0")
    // Not used directly (no fragments) — pins the transitive androidx.fragment
    // above 1.3.0; older ones break the Activity Result API (lint: fatal).
    implementation("androidx.fragment:fragment-ktx:1.9.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.11.0")
    implementation("androidx.work:work-runtime-ktx:2.11.2")
    implementation("com.google.android.gms:play-services-location:21.4.0")
    implementation("com.squareup.okhttp3:okhttp:5.5.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.11.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-play-services:1.11.0")
    // In-app QR scanner: a cold-started, signed-out app must be able to redeem
    // the setup QR itself — however the person opened it (installer's "Open",
    // the icon), the answer is one button: scan the QR again.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    // Native push (FCM) — Web Push has no WebView support, so the app receives
    // notifications through Firebase Cloud Messaging instead.
    implementation(platform("com.google.firebase:firebase-bom:34.19.0"))
    implementation("com.google.firebase:firebase-messaging")
}
