package com.backbubbles.bridge

import android.Manifest
import android.content.*
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/**
 * BackBubbles Bridge — MainActivity
 *
 * Provides a minimal setup UI:
 *  • Displays and persists the WebSocket port.
 *  • Shows a Start / Stop button for the foreground [BridgeService].
 *  • Requests SMS and notification permissions on first launch.
 */
class MainActivity : AppCompatActivity() {

    companion object {
        private const val PREFS_NAME = "backbubbles"
        private const val KEY_PORT = "port"
        private const val DEFAULT_PORT = 8765

        private const val REQ_PERMISSIONS = 1001
        private val REQUIRED_PERMISSIONS = buildList {
            add(Manifest.permission.SEND_SMS)
            add(Manifest.permission.RECEIVE_SMS)
            add(Manifest.permission.READ_SMS)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                add(Manifest.permission.POST_NOTIFICATIONS)
            }
        }.toTypedArray()
    }

    private lateinit var portEdit: EditText
    private lateinit var statusText: TextView
    private lateinit var startStopButton: Button
    private lateinit var ipText: TextView

    private val serviceStateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.action) {
                BridgeService.ACTION_STATE_CHANGED -> updateServiceStateUi(
                    intent.getBooleanExtra(BridgeService.EXTRA_RUNNING, false),
                    intent.getStringExtra(BridgeService.EXTRA_STATUS) ?: ""
                )
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Build a simple vertical layout programmatically to avoid extra XML files.
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 80, 48, 48)
        }

        val title = TextView(this).apply {
            text = "BackBubbles Bridge"
            textSize = 22f
        }
        layout.addView(title)

        val portLabel = TextView(this).apply {
            text = "\nWebSocket port:"
        }
        layout.addView(portLabel)

        portEdit = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(getPrefs().getInt(KEY_PORT, DEFAULT_PORT).toString())
        }
        layout.addView(portEdit)

        ipText = TextView(this).apply {
            text = "Device IP: ${NetworkUtils.getLocalIpAddress()}"
            setPadding(0, 16, 0, 0)
        }
        layout.addView(ipText)

        statusText = TextView(this).apply {
            text = "\nService status: stopped"
        }
        layout.addView(statusText)

        startStopButton = Button(this).apply {
            text = "Start Bridge"
            setOnClickListener { onStartStopClicked() }
        }
        layout.addView(startStopButton)

        setContentView(layout)
        requestMissingPermissions()
    }

    override fun onResume() {
        super.onResume()
        val filter = IntentFilter(BridgeService.ACTION_STATE_CHANGED)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(serviceStateReceiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(serviceStateReceiver, filter)
        }
        // Ask service for its current state.
        sendBroadcast(Intent(BridgeService.ACTION_QUERY_STATE))
    }

    override fun onPause() {
        super.onPause()
        unregisterReceiver(serviceStateReceiver)
    }

    // ------------------------------------------------------------------

    private fun onStartStopClicked() {
        val running = startStopButton.tag as? Boolean ?: false
        if (running) {
            stopService(Intent(this, BridgeService::class.java))
        } else {
            val port = portEdit.text.toString().toIntOrNull()?.coerceIn(1, 65535) ?: DEFAULT_PORT
            getPrefs().edit().putInt(KEY_PORT, port).apply()
            val intent = Intent(this, BridgeService::class.java).apply {
                putExtra(BridgeService.EXTRA_PORT, port)
            }
            ContextCompat.startForegroundService(this, intent)
        }
    }

    private fun updateServiceStateUi(running: Boolean, status: String) {
        startStopButton.tag = running
        startStopButton.text = if (running) "Stop Bridge" else "Start Bridge"
        statusText.text = "\nService status: ${if (running) "running" else "stopped"}" +
                if (status.isNotEmpty()) "\n$status" else ""
    }

    private fun requestMissingPermissions() {
        val missing = REQUIRED_PERMISSIONS.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, missing.toTypedArray(), REQ_PERMISSIONS)
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_PERMISSIONS) {
            val denied = permissions.zip(grantResults.toList()).filter {
                it.second != PackageManager.PERMISSION_GRANTED
            }.map { it.first }
            if (denied.isNotEmpty()) {
                Toast.makeText(
                    this,
                    "Some permissions were denied: ${denied.joinToString()}",
                    Toast.LENGTH_LONG
                ).show()
            }
        }
    }

    private fun getPrefs(): SharedPreferences =
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
}
