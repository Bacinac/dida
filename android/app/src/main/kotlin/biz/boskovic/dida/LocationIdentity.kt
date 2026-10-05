package biz.boskovic.dida

import java.net.URI
import java.util.Locale

data class LocationIdentity(val userId: String, val origin: String) {
    companion object {
        fun origin(url: String): String? = runCatching {
            val uri = URI(url)
            if (uri.scheme?.lowercase(Locale.ROOT) != "https" || uri.host == null || uri.rawUserInfo != null) return null
            val port = uri.port
            if (port != -1 && port !in 1..65535) return null
            val host = uri.host.lowercase(Locale.ROOT)
            val authority = if (host.contains(':') && !host.startsWith('[')) "[$host]" else host
            "https://$authority" + if (port == -1 || port == 443) "" else ":$port"
        }.getOrNull()
    }
}

data class LocationStamp(val revision: String, val observedAtMs: Long, val sequence: Long) {
    fun belongsTo(currentRevision: String?): Boolean = revision == currentRevision
}
