import os
import base64
from openai import OpenAI

# 配置硅基流动的API
client = OpenAI(
    api_key="sk-lxeewliusahjaucqxkmnvgswytrvtwonommtvyeywuzeuibz",  # 请设置环境变量 SILICONFLOW_API_KEY
    base_url="https://api.siliconflow.cn/v1"
)

# 音频文件路径（请修改为您的实际路径）
audio_path = "/Users/edenfwu/Downloads/YTDown.com_Shorts_Did-we-just-get-stereotyped_Media_cF2JGEzcwWo_001_1080p_1.wav"

# 读取并编码音频文件
def encode_audio(audio_path):
    with open(audio_path, "rb") as audio_file:
        return base64.b64encode(audio_file.read()).decode('utf-8')

# 获取音频的MIME类型
def get_mime_type(audio_path):
    ext = os.path.splitext(audio_path)[1].lower()
    mime_types = {
        '.mp3': 'audio/mpeg',
        '.wav': 'audio/wav',
        '.m4a': 'audio/mp4',
        '.ogg': 'audio/ogg',
        '.flac': 'audio/flac',
        '.mp4': 'audio/mp4',
    }
    return mime_types.get(ext, 'audio/mpeg')

# 编码音频
audio_base64 = encode_audio(audio_path)
mime_type = get_mime_type(audio_path)

# 调用API
try:
    response = client.chat.completions.create(
        model="Qwen/Qwen3-Omni-30B-A3B-Instruct",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "请输出srt字幕，并标上说话人编号，注意时间戳要准确，格式要正确，输出的字幕内容要与音频内容一致。"
                    },
                    {
                        "type": "audio_url",
                        "audio_url": {
                            "url": f"data:{mime_type};base64,{audio_base64}"
                        }
                    }
                ]
            }
        ],
        max_tokens=4096,
        temperature=0.7
    )
    
    print("=== 生成的SRT字幕 ===")
    print(response.choices[0].message.content)
    
except Exception as e:
    print(f"错误: {e}")
