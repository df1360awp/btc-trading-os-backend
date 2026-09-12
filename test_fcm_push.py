from market.fcm_sender import send_to_active_devices


result = send_to_active_devices(
    {
        "title": "BTC Trading OS 测试",
        "body": "FCM 推送测试成功"
    }
)

print("FCM push result:")
print(result)
