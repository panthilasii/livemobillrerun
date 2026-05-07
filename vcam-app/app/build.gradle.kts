plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.livemobillrerun.vcam"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.livemobillrerun.vcam"
        minSdk = 33
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }

    packaging {
        resources {
            pickFirsts += setOf(
                "META-INF/*",
                "META-INF/**/*",
                "**/*.kotlin_module",
                "kotlin-tooling-metadata.json",
            )
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")

    // Xposed API. compileOnly because it must NOT be packaged into the
    // APK; LSPosed provides it at runtime when the module is loaded
    // into TikTok's process. If it ends up in the APK, both copies fight
    // for the same class names and the hook silently fails.
    compileOnly("de.robv.android.xposed:api:82")
}
