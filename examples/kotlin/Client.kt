// Minimal Claude Sidecar client (Kotlin / JVM).
//
// Streams SSE events from /v1/converse via the Ktor client's SSE plugin and
// prints them. Demonstrates the HTTP+SSE contract — the sidecar's whole
// reason for being. Build with the bundled build.gradle.kts.
//
// Usage:
//   BEARER_SECRET=... ./gradlew run --args='hello'

import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.plugins.sse.SSE
import io.ktor.client.plugins.sse.sse
import io.ktor.client.request.header
import io.ktor.client.request.setBody
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.contentType
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

@Serializable
data class ConverseRequest(val sessionKey: String, val prompt: String)

fun main(args: Array<String>) = runBlocking {
    val prompt = args.firstOrNull() ?: error("usage: <prompt>")
    val sidecarUrl = System.getenv("SIDECAR_URL") ?: "http://127.0.0.1:7300"
    val bearer = System.getenv("BEARER_SECRET") ?: error("BEARER_SECRET unset")

    val client = HttpClient(CIO) {
        // A turn streams for up to TURN_TIMEOUT_SEC — disable CIO's 15 s default.
        engine { requestTimeout = 0 }
        install(SSE)
    }

    client.use {
        it.sse(
            urlString = "$sidecarUrl/v1/converse",
            request = {
                method = HttpMethod.Post
                header(HttpHeaders.Authorization, "Bearer $bearer")
                contentType(ContentType.Application.Json)
                setBody(Json.encodeToString(ConverseRequest(sessionKey = "demo:kotlin", prompt = prompt)))
            },
        ) {
            incoming.collect { event ->
                println("[${event.event ?: "?"}] ${event.data}")
            }
        }
    }
}
