
import { GoogleGenAI, Modality, Type } from "@google/genai";

const ai = new GoogleGenAI({ apiKey: process.env.API_KEY });

const fileToBase64 = (file: File): Promise<string> => {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.readAsDataURL(file);
        reader.onload = () => {
            const base64String = (reader.result as string).split(',')[1];
            resolve(base64String);
        };
        reader.onerror = reject;
    });
};

export async function* transcribeVideoStreaming(file: File, modelName: string) {
  const base64Data = await fileToBase64(file);
  
  // Updated contents to use the recommended parts structure for multimodal input
  const responseStream = await ai.models.generateContentStream({
    model: modelName,
    contents: {
      parts: [
        {
          inlineData: {
            mimeType: file.type,
            data: base64Data,
          },
        },
        {
          text: `Transcribe this video audio. Identify different speakers. 
                 Output segments one by one as they are found.
                 Format each segment as a single valid JSON object on its own line, prefixed with "SEGMENT:".
                 Each segment MUST have: startTime (number), endTime (number), speakerLabel (string), and text (string).
                 Example:
                 SEGMENT:{"startTime": 0.5, "endTime": 2.1, "speakerLabel": "Speaker 1", "text": "Hello world"}`,
        }
      ]
    },
  });

  let buffer = "";
  for await (const chunk of responseStream) {
    buffer += chunk.text;
    const lines = buffer.split('\n');
    buffer = lines.pop() || ""; // Keep incomplete line in buffer

    for (const line of lines) {
      if (line.trim().startsWith("SEGMENT:")) {
        try {
          const jsonStr = line.trim().substring(8);
          yield JSON.parse(jsonStr);
        } catch (e) {
          console.warn("Failed to parse segment chunk", e);
        }
      }
    }
  }
}

export const translateText = async (text: string, targetLanguage: string, modelName: string): Promise<string> => {
  const response = await ai.models.generateContent({
    model: modelName,
    contents: `Translate the following text to ${targetLanguage}. Provide ONLY the translation: "${text}"`,
  });
  return response.text.trim();
};

export const translateWholeScript = async (
    segments: {id: string, text: string, speakerId: string, startTime: number}[], 
    targetLanguage: string, 
    modelName: string,
    onStreamUpdate?: (chunk: string) => void
): Promise<{id: string, translatedText: string}[]> => {
    // Prepare a concise context payload
    const scriptContext = segments.map(s => ({
        id: s.id,
        time: s.startTime,
        speaker: s.speakerId,
        text: s.text
    }));

    const prompt = `
    You are a professional video translator. 
    Translate the following script to ${targetLanguage}. 
    Use the provided time and speaker context to ensure the translation flows naturally and maintains the correct tone.
    
    IMPORTANT: Return the output as a STRICT JSON ARRAY of objects. 
    Each object must have exactly two properties: "id" (matching the input) and "translatedText".
    Do not wrap the JSON in markdown code blocks. Just return the raw JSON string.

    Input Script:
    ${JSON.stringify(scriptContext, null, 2)}
    `;

    try {
        const responseStream = await ai.models.generateContentStream({
            model: modelName,
            contents: prompt,
            config: {
                responseMimeType: 'application/json'
            }
        });
        
        let fullText = "";
        for await (const chunk of responseStream) {
            const text = chunk.text;
            if (text) {
                fullText += text;
                if (onStreamUpdate) onStreamUpdate(text);
            }
        }

        // Remove markdown code blocks if present just in case
        const cleanJson = fullText.replace(/^```json\s*|\s*```$/g, '');
        // Handle potential lingering markdown or whitespace
        const start = cleanJson.indexOf('[');
        const end = cleanJson.lastIndexOf(']');
        if (start === -1 || end === -1) {
            throw new Error("Invalid JSON response format");
        }
        const jsonStr = cleanJson.substring(start, end + 1);
        
        return JSON.parse(jsonStr);
    } catch (e) {
        console.error("Batch translation failed", e);
        throw e;
    }
};

export const generateGeminiSpeech = async (text: string, voiceName: string, modelName: string): Promise<string | null> => {
    try {
        const response = await ai.models.generateContent({
            model: modelName,
            contents: { parts: [{ text }] },
            config: {
                responseModalities: [Modality.AUDIO],
                speechConfig: {
                    voiceConfig: {
                        prebuiltVoiceConfig: { voiceName: voiceName },
                    },
                },
            },
        });
        // Accessing audio data from the first part's inlineData
        return response.candidates?.[0]?.content?.parts?.[0]?.inlineData?.data || null;
    } catch (error) {
        console.error("Gemini TTS Error:", error);
        return null;
    }
};
