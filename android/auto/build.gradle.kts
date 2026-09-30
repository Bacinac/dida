import java.util.Properties

plugins {
    id("com.android.application")
}

val didaHost: String = (project.findProperty("didaHost") as String?)
    ?: error("didaHost is not set: build through android/build.sh, which takes it from keystore/keystore.properties")
val verCode: Int = ((project.findProperty("versionCode") as String?) ?: "1").toInt()
val verName: String = (project.findProperty("versionName") as String?) ?: "v0.0.0-dev"

android {
    namespace = "biz.boskovic.dida.auto"
    compileSdk {
        version = release(37) { minorApiLevel = 2 }
    }
    buildToolsVersion = "37.0.0"

    defaultConfig {
        applicationId = "biz.boskovic.dida.auto"
        minSdk = 26
        targetSdk = 36
        versionCode = verCode
        versionName = verName
        resValue("string", "app_ver", verName)
        resValue("string", "default_base_url", "https://$didaHost")
    }

    lint {
        // targetSdk stays below 37: API 37 makes local-network access a runtime
        // permission, and the deployment host resolves to the LAN at home.
        disable += "OldTargetApi"
        checkDependencies = true
    }

    buildFeatures {
        resValues = true
    }

    signingConfigs {
        create("release") {
            // Shares the dida keystore; for Play it acts as the upload key —
            // Play App Signing holds the distribution key.
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
    implementation(project(":shared"))
    implementation("androidx.car.app:app:1.7.0")
}
