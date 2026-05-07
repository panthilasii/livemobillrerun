pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // The Xposed API artifact (de.robv.android.xposed:api:82) only
        // lives on the original Xposed mirror; nothing else.
        maven { url = uri("https://api.xposed.info/") }
    }
}

rootProject.name = "vcam-app"
include(":app")
