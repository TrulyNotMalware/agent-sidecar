plugins {
    application
    kotlin("jvm") version "2.0.0"
}

repositories { mavenCentral() }

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:okhttp-sse:4.12.0")
}

application {
    mainClass.set("ClientKt")
}
