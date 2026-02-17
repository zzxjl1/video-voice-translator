import requests
import json
import os
import time

# 若没有配置环境变量，请用百炼API Key将下行替换为：api_key = "sk-xxx"
api_key = "sk-22b943ff3e5c499abbfbfb33a7cf4451"
file_urls = [
    "http://bt-panel.idealbroker.cn:8888/down/a6POpvM3CvVH.mp3",
]


# 提交文件转写任务，包含待转写文件url列表
def submit_task(apikey, file_urls) -> str:

    headers = {
        "Authorization": f"Bearer {apikey}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    data = {
        "model": "fun-asr",
        "input": {"file_urls": file_urls},
        "parameters": {
            "channel_id": [0],
            "diarization_enabled": True
        },
    }
    # 录音文件转写服务url
    service_url = (
        "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
    )
    response = requests.post(
        service_url, headers=headers, data=json.dumps(data)
    )

    # 打印响应内容
    if response.status_code == 200:
        return response.json()["output"]["task_id"]
    else:
        print("task failed!")
        print(response.json())
        return None


# 循环查询任务状态直到成功
def wait_for_complete(task_id):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }

    pending = True
    while pending:
        # 查询任务状态服务url
        service_url = f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
        response = requests.post(
            service_url, headers=headers
        )
        if response.status_code == 200:
            status = response.json()['output']['task_status']
            if status == 'SUCCEEDED':
                print("task succeeded!")
                pending = False
                return response.json()['output']['results']
            elif status == 'RUNNING' or status == 'PENDING':
                pass
            else:
                print("task failed!")
                pending = False
        else:
            print("query failed!")
            pending = False
        print(response.json())
        time.sleep(0.1)


task_id = submit_task(apikey=api_key, file_urls=file_urls)
print("task_id: ", task_id)
result = wait_for_complete(task_id)
print("transcription result: ", result)