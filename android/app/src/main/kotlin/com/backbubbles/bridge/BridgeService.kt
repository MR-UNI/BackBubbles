package com.backbubbles.bridge

import android.app.*
import android.content.*
import android.os.*
import android.util.Log
import androidx.core.app.NotificationCompat
import okhttp3.*
import org.json.JSONException
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * BackBubbles Bridge — BridgeService
 *
 * A foreground service that runs a WebSocket *server* (via OkHttp's
 * WebSocket client pointed at the Mac helper acting as the WebSocket server,
 * or, in local-server mode, listens for the Mac helper's connection).
 *
 * Because OkHttp ships a WebSocket *client* (not server), BackBubbles uses
 * a simple role-reversal: the **Mac helper** acts as the WebSocket *server*
 * and the Android app connects to it as a client.  This avoids needing a
 * third-party WebSocket server library on Android and keeps the network
 * hole-punching simple (the Mac's IP is stable on a local network).
 *
 * Message protocol — see [bridge_client.py] docstring for the full spec.
 */
class BridgeService : Service() {

    companion object {
        private const val TAG = "BridgeService"
        private const val CHANNEL_ID = "backbubbles_bridge"
        private const val NOTIF_ID = 1

        const val EXTRA_PORT = "port"
        const val EXTRA_RUNNING = "running"
        const val EXTRA_STATUS = "status"
        const val EXTRA_FROM = "from"
        const val EXTRA_BODY = "body"
        const val EXTRA_SERVICE_TYPE = "service_type"
        const val EXTRA_TIMESTAMP = "timestamp"
        const val ACTION_STATE_CHANGED = "com.backbubbles.STATE_CHANGED"
        const val ACTION_QUERY_STATE = "com.backbubbles.QUERY_STATE"
        const val ACTION_FORWARD_INCOMING = "com.backbubbles.FORWARD_INCOMING"

        private const val PREFS_NAME = "backbubbles"
        private const val KEY_MAC_HOST = "mac_host"
        private const val KEY_PORT = "port"
        private const val DEFAULT_PORT = 8765

        // How long to wait before reconnecting on disconnect (ms).
        private const val RECONNECT_DELAY_MS = 5_000L
    }

    private var webSocket: WebSocket? = null
    private var running = false
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var smsSender: SmsSender
    private var macHost: String = ""
    private var port: Int = DEFAULT_PORT

    private val queryStateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            broadcastState()
        }
    }

    // ------------------------------------------------------------------
    // Service lifecycle
    // ------------------------------------------------------------------

    override fun onCreate() {
        super.onCreate()
        smsSender = SmsSender(this)
        createNotificationChannel()
        val filter = IntentFilter(ACTION_QUERY_STATE)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(queryStateReceiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(queryStateReceiver, filter)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_FORWARD_INCOMING -> {
                // Incoming SMS relayed here from SmsReceiver.
                val from = intent.getStringExtra(EXTRA_FROM) ?: return START_NOT_STICKY
                val body = intent.getStringExtra(EXTRA_BODY) ?: return START_NOT_STICKY
                val serviceType = intent.getStringExtra(EXTRA_SERVICE_TYPE) ?: "SMS"
                val timestamp = intent.getLongExtra(EXTRA_TIMESTAMP, System.currentTimeMillis())
                sendIncomingSmsToMac(from, body, serviceType, timestamp)
                return START_NOT_STICKY
            }
            else -> {
                // Normal start from MainActivity.
                port = intent?.getIntExtra(EXTRA_PORT, DEFAULT_PORT) ?: DEFAULT_PORT
                macHost = getPrefs().getString(KEY_MAC_HOST, "") ?: ""
                startForeground(NOTIF_ID, buildNotification("Connecting…"))
                running = true
                connectToMac()
                broadcastState()
                return START_STICKY
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        running = false
        handler.removeCallbacksAndMessages(null)
        webSocket?.close(1000, "Service stopped")
        webSocket = null
        unregisterReceiver(queryStateReceiver)
        broadcastState()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ------------------------------------------------------------------
    // WebSocket connection
    // ------------------------------------------------------------------

    private fun connectToMac() {
        val host = macHost.ifEmpty {
            // Fall back to a broadcast / mDNS address if host not configured.
            Log.w(TAG, "mac_host not set; waiting for configuration.")
            scheduleReconnect()
            return
        }
        val url = "ws://$host:$port"
        Log.i(TAG, "Connecting to Mac helper at $url")

        val client = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)  // keep-alive
            .pingInterval(20, TimeUnit.SECONDS)
            .build()

        val request = Request.Builder().url(url).build()
        webSocket = client.newWebSocket(request, object : WebSocketListener() {

            override fun onOpen(ws: WebSocket, response: Response) {
                Log.i(TAG, "Connected to Mac helper.")
                updateNotification("Connected to Mac helper")
                broadcastState("Connected to Mac helper at $host:$port")
            }

            override fun onMessage(ws: WebSocket, text: String) {
                handleIncoming(text)
            }

            override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                ws.close(1000, null)
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "WebSocket failure: ${t.message}")
                webSocket = null
                updateNotification("Disconnected — reconnecting…")
                broadcastState("Disconnected — reconnecting…")
                if (running) scheduleReconnect()
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "WebSocket closed: $code $reason")
                webSocket = null
                if (running) scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        handler.postDelayed({ if (running) connectToMac() }, RECONNECT_DELAY_MS)
    }

    // ------------------------------------------------------------------
    // Incoming messages from Mac helper
    // ------------------------------------------------------------------

    private fun handleIncoming(raw: String) {
        val json = try {
            JSONObject(raw)
        } catch (e: JSONException) {
            Log.w(TAG, "Non-JSON frame from Mac: $raw")
            return
        }

        when (json.optString("type")) {
            "send_sms" -> {
                val messageId = json.optString("message_id")
                val to = json.optString("to")
                val body = json.optString("body")
                val service = json.optString("service", "SMS")
                Log.i(TAG, "Sending $service to=$to (id=$messageId)")
                smsSender.send(
                    to = to,
                    body = body,
                    messageId = messageId,
                    onSent = { id, success ->
                        sendStatusToMac(id, if (success) "sent" else "failed")
                    },
                    onDelivered = { id ->
                        sendStatusToMac(id, "delivered")
                    },
                )
            }
            else -> Log.w(TAG, "Unknown message type: ${json.optString("type")}")
        }
    }

    // ------------------------------------------------------------------
    // Send delivery status back to Mac
    // ------------------------------------------------------------------

    private fun sendStatusToMac(messageId: String, status: String, errorCode: Int = 4) {
        val payload = JSONObject().apply {
            put("type", "delivery_status")
            put("message_id", messageId)
            put("status", status)
            if (status == "failed") put("error_code", errorCode)
        }.toString()
        val sent = webSocket?.send(payload) ?: false
        if (!sent) Log.w(TAG, "Could not send delivery status — WebSocket not open.")
    }

    /**
     * Forward an incoming SMS from [SmsReceiver] to the Mac helper.
     */
    private fun sendIncomingSmsToMac(from: String, body: String, service: String, timestamp: Long) {
        val payload = JSONObject().apply {
            put("type", "incoming_sms")
            put("from", from)
            put("body", body)
            put("service", service)
            put("timestamp", timestamp / 1000.0)
        }.toString()
        val sent = webSocket?.send(payload) ?: false
        if (!sent) Log.w(TAG, "Could not forward incoming SMS — WebSocket not open.")
    }

    // ------------------------------------------------------------------
    // State broadcast
    // ------------------------------------------------------------------

    private fun broadcastState(status: String = "") {
        sendBroadcast(Intent(ACTION_STATE_CHANGED).apply {
            putExtra(EXTRA_RUNNING, running)
            putExtra(EXTRA_STATUS, status)
        })
    }

    // ------------------------------------------------------------------
    // Notification helpers
    // ------------------------------------------------------------------

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "BackBubbles Bridge",
                NotificationManager.IMPORTANCE_LOW
            ).apply { description = "SMS relay service" }
            getSystemService(NotificationManager::class.java)
                ?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(text: String): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("BackBubbles Bridge")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm?.notify(NOTIF_ID, buildNotification(text))
    }

    private fun getPrefs(): SharedPreferences =
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
}
