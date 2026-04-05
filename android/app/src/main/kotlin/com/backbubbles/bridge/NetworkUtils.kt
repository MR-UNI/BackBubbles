package com.backbubbles.bridge

import android.content.Context
import android.net.wifi.WifiManager
import java.net.NetworkInterface

/**
 * Utility helpers for network-related tasks.
 */
object NetworkUtils {

    /**
     * Return a human-readable local IP address (IPv4 preferred), or
     * ``"unknown"`` if one cannot be determined.
     */
    fun getLocalIpAddress(): String {
        return try {
            NetworkInterface.getNetworkInterfaces()
                ?.asSequence()
                ?.flatMap { it.inetAddresses.asSequence() }
                ?.firstOrNull { !it.isLoopbackAddress && it.hostAddress?.contains(':') == false }
                ?.hostAddress
                ?: "unknown"
        } catch (_: Exception) {
            "unknown"
        }
    }
}
