// Minimal Claude Sidecar client (Kotlin / JVM).
//
// Streams SSE events from /v1/converse via OkHttp's EventSource and prints
// them. Demonstrates the HTTP+SSE contract — the sidecar's whole reason for
// being. Build with the bundled build.gradle.kts.
//
// Usage:
//   BEARER_SECRET=... ./gradlew run --args='hello'

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import java.time.Duration
import java.util.concurrent.CountDownLatch

private fun jsonEscape(s: String): String =
    s.replace("\\", "\\\\").replace("\"", "\\\"")

fun main(args: Array<String>) {
    val prompt = args.firstOrNull() ?: error("usage: <prompt>")
    val sidecarUrl = System.getenv("SIDECAR_URL") ?: "http://127.0.0.1:7300"
    val bearer = System.getenv("BEARER_SECRET") ?: error("BEARER_SECRET unset")

    val payload = """{"sessionKey":"demo:kotlin","prompt":"${jsonEscape(prompt)}"}"""

    val client = OkHttpClient.Builder()
        .readTimeout(Duration.ofMinutes(5))
        .build()

    val request = Request.Builder()
        .url("$sidecarUrl/v1/converse")
        .header("Authorization", "Bearer $bearer")
        .header("Accept", "text/event-stream")
        .post(payload.toRequestBody("application/json".toMediaType()))
        .build()

    val done = CountDownLatch(1)
    EventSources.createFactory(client).newEventSource(
        request,
        object : EventSourceListener() {
            override fun onEvent(es: EventSource, id: String?, type: String?, data: String) {
                println("[${type ?: "?"}] $data")
            }
            override fun onClosed(es: EventSource) { done.countDown() }
            override fun onFailure(es: EventSource, t: Throwable?, response: Response?) {
                t?.printStackTrace()
                done.countDown()
            }
        },
    )
    done.await()
}
