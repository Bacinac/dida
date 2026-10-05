package biz.boskovic.dida

internal object OwnTracksUrls {
    fun appStatus(postUrl: String, path: String): String? {
        if (!postUrl.endsWith("/api/owntracks") || !path.startsWith("/app/")) return null
        return postUrl.removeSuffix("/owntracks") + path
    }
}
