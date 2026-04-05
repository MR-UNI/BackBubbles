package com.backbubbles.bridge

import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.telephony.SmsManager
import android.util.Log

/**
 * BackBubbles Bridge — SmsSender
 *
 * Wraps [SmsManager] to send SMS messages and report sent/delivered
 * status back via callbacks.
 *
 * Long messages are automatically split by the system SmsManager.
 * Each part registers its own PendingIntent for sent/delivery confirmation.
 */
class SmsSender(private val context: Context) {

    companion object {
        private const val TAG = "SmsSender"
        private const val ACTION_SMS_SENT = "com.backbubbles.SMS_SENT"
        private const val ACTION_SMS_DELIVERED = "com.backbubbles.SMS_DELIVERED"
        private const val EXTRA_MESSAGE_ID = "message_id"
        private const val EXTRA_PART_COUNT = "part_count"
        private const val EXTRA_PART_INDEX = "part_index"
    }

    /**
     * Send an SMS to [to] with content [body].
     *
     * @param messageId  The GUID used by the Mac helper to track the message.
     * @param onSent     Called once *all* parts have been handed to the radio.
     *                   [success] is false if *any* part failed.
     * @param onDelivered Called once *all* parts have been delivered to the handset.
     */
    fun send(
        to: String,
        body: String,
        messageId: String,
        onSent: (messageId: String, success: Boolean) -> Unit,
        onDelivered: (messageId: String) -> Unit,
    ) {
        val smsManager = getSmsManager()
        val parts = smsManager.divideMessage(body)
        val partCount = parts.size

        // Track part completion.
        val sentResults = IntArray(partCount) { -1 }      // -1 = pending
        val deliveredCount = IntArray(1) { 0 }

        val sentReceiver = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context, intent: Intent) {
                val partIndex = intent.getIntExtra(EXTRA_PART_INDEX, 0)
                sentResults[partIndex] = resultCode
                val allDone = sentResults.none { it == -1 }
                if (allDone) {
                    context.unregisterReceiver(this)
                    val success = sentResults.all { it == Activity.RESULT_OK }
                    if (!success) {
                        Log.w(TAG, "SMS send failure for message $messageId (codes: ${sentResults.toList()})")
                    }
                    onSent(messageId, success)
                }
            }
        }

        val deliveredReceiver = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context, intent: Intent) {
                deliveredCount[0]++
                if (deliveredCount[0] >= partCount) {
                    context.unregisterReceiver(this)
                    onDelivered(messageId)
                }
            }
        }

        val sentAction = "$ACTION_SMS_SENT.$messageId"
        val deliveredAction = "$ACTION_SMS_DELIVERED.$messageId"

        val sentFilter = IntentFilter(sentAction)
        val deliveredFilter = IntentFilter(deliveredAction)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.registerReceiver(sentReceiver, sentFilter, Context.RECEIVER_NOT_EXPORTED)
            context.registerReceiver(deliveredReceiver, deliveredFilter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            context.registerReceiver(sentReceiver, sentFilter)
            @Suppress("UnspecifiedRegisterReceiverFlag")
            context.registerReceiver(deliveredReceiver, deliveredFilter)
        }

        val sentIntents = ArrayList<PendingIntent>(partCount)
        val deliveredIntents = ArrayList<PendingIntent>(partCount)

        for (i in 0 until partCount) {
            val sentIntent = PendingIntent.getBroadcast(
                context,
                messageId.hashCode() * 1000 + i,
                Intent(sentAction).apply {
                    putExtra(EXTRA_MESSAGE_ID, messageId)
                    putExtra(EXTRA_PART_COUNT, partCount)
                    putExtra(EXTRA_PART_INDEX, i)
                    setPackage(context.packageName)
                },
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
            )
            val deliveredIntent = PendingIntent.getBroadcast(
                context,
                messageId.hashCode() * 1000 + i + 500,
                Intent(deliveredAction).apply {
                    putExtra(EXTRA_MESSAGE_ID, messageId)
                    putExtra(EXTRA_PART_INDEX, i)
                    setPackage(context.packageName)
                },
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
            )
            sentIntents.add(sentIntent)
            deliveredIntents.add(deliveredIntent)
        }

        try {
            smsManager.sendMultipartTextMessage(to, null, parts, sentIntents, deliveredIntents)
            Log.i(TAG, "sendMultipartTextMessage called: to=$to parts=$partCount id=$messageId")
        } catch (e: Exception) {
            Log.e(TAG, "sendMultipartTextMessage failed for $messageId", e)
            context.unregisterReceiver(sentReceiver)
            context.unregisterReceiver(deliveredReceiver)
            onSent(messageId, false)
        }
    }

    // ------------------------------------------------------------------

    @Suppress("DEPRECATION")
    private fun getSmsManager(): SmsManager {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            context.getSystemService(SmsManager::class.java)
        } else {
            SmsManager.getDefault()
        }
    }
}
