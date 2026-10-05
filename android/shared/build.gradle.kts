plugins {
    id("com.android.library")
}

android {
    namespace = "biz.boskovic.dida.shared"
    compileSdk {
        version = release(37) { minorApiLevel = 2 }
    }
    buildToolsVersion = "37.0.0"

    defaultConfig {
        minSdk = 26
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
    api("androidx.core:core-ktx:1.19.1")
    api("androidx.activity:activity-ktx:1.13.0")
    api("androidx.lifecycle:lifecycle-runtime-ktx:2.11.0")
    api("com.squareup.okhttp3:okhttp:5.5.0")
    api("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.11.0")
}
