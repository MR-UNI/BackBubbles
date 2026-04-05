package com.backbubbles.bridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Telephony
import android.telephony.SmsMessage
import android.util.Log

/**
 * BackBubbles Bridge — SmsReceiver
 *
 * Listens for incoming SMS messages (``android.provider.Telephony.SMS_RECEIVED``)
 * and forwards them to [BridgeService] so they can be relayed to the Mac helper
 * for injection into chat.db.
 *
 * This receiver is declared statically in [AndroidManifest.xml] so it wakes
 * the app even when it is not running.
 */
class SmsReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "SmsReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return

        val messages: Array<SmsMessage> = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT) {
            Telephony.Sms.Intents.getMessagesFromIntent(intent)
        } else {
            @Suppress("DEPRECATION")
            getSmsFromBundleLegacy(intent)
        } ?: return

        if (messages.isEmpty()) return

        // Group PDUs by originating address into a single logical message.
        val grouped = mutableMapOf<String, StringBuilder>()
        var timestamp = 0L

        for (msg in messages) {
            val sender = msg.displayOriginatingAddress ?: continue
            grouped.getOrPut(sender) { StringBuilder() }.append(msg.messageBody ?: "")
            if (timestamp == 0L) timestamp = msg.timestampMillis
        }

        // Start (or reach) BridgeService and forward each message.
        for ((sender, bodyBuilder) in grouped) {
            val body = bodyBuilder.toString()
            Log.i(TAG, "Incoming SMS from $sender: ${body.take(40)}")

            // If the service is running, we can call it directly via a bound
            // reference; here we use a simpler approach: send a broadcast that
            // BridgeService can pick up, keeping SmsReceiver decoupled.
            val forwardIntent = Intent(context, BridgeService::class.java).apply {
                action = BridgeService.ACTION_FORWARD_INCOMING
                putExtra(BridgeService.EXTRA_FROM, sender)
                putExtra(BridgeService.EXTRA_BODY, body)
                putExtra(BridgeService.EXTRA_SERVICE_TYPE, "SMS")
                putExtra(BridgeService.EXTRA_TIMESTAMP, timestamp)
            }
            // Use startService (not startForegroundService) — if the service
            // isn't running we don't want to force-start a foreground service
            // purely for receiving; the message will be handled if the service
            // is already up.
            context.startService(forwardIntent)
        }
    }

    @Suppress("DEPRECATION")
    private fun getSmsFromBundleLegacy(intent: Intent): Array<SmsMessage>? {
        val pdus = intent.extras?.get("pdus") as? Array<*> ?: return null
        return pdus.mapNotNull { pdu ->
            SmsMessage.createFromPdu(pdu as ByteArray)
        }.toTypedArray()
    }
}
