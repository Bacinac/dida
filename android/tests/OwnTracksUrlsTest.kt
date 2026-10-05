package biz.boskovic.dida

fun main() {
    for (origin in listOf("https://home.example", "https://home.example:8443")) {
        val provisioned = "$origin/api/owntracks"
        for (path in listOf("/app/location-status", "/app/push-image")) {
            val diagnostic = OwnTracksUrls.appStatus(provisioned, path)
            check(diagnostic == "$origin/api$path")
            val proxyPath = java.net.URI(diagnostic).rawPath
            check(proxyPath.startsWith("/api/"))
            check(proxyPath.removePrefix("/api") == path)
        }
    }
    check(OwnTracksUrls.appStatus("https://home.example/owntracks", "/app/location-status") == null)
    check(OwnTracksUrls.appStatus("https://home.example/api/owntracks/", "/app/location-status") == null)
    check(OwnTracksUrls.appStatus("https://home.example/api/owntracks", "/location-status") == null)
    println("OwnTracks diagnostic URL contract passed")
}
