
export const generateThumbnails = async (file: File, count: number = 10): Promise<string[]> => {
  return new Promise((resolve, reject) => {
    const video = document.createElement('video');
    const objectUrl = URL.createObjectURL(file);
    video.src = objectUrl;
    video.muted = true;
    video.playsInline = true;
    
    const thumbnails: string[] = [];
    
    video.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      reject(new Error("Video format not supported for preview generation."));
    };

    video.onloadedmetadata = async () => {
      const duration = video.duration;
      if (!duration || isNaN(duration)) {
        URL.revokeObjectURL(objectUrl);
        return resolve([]);
      }

      const interval = duration / count;
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      
      try {
        for (let i = 0; i < count; i++) {
          video.currentTime = i * interval;
          await new Promise((r, rej) => {
            const timeout = setTimeout(() => rej("Thumbnail timeout"), 2000);
            const onSeeked = () => {
              clearTimeout(timeout);
              video.removeEventListener('seeked', onSeeked);
              canvas.width = 160;
              canvas.height = 90;
              ctx?.drawImage(video, 0, 0, canvas.width, canvas.height);
              thumbnails.push(canvas.toDataURL('image/jpeg', 0.5));
              r(null);
            };
            video.addEventListener('seeked', onSeeked);
          });
        }
      } catch (e) {
        console.warn("Thumbnail generation partially failed:", e);
      } finally {
        URL.revokeObjectURL(objectUrl);
        resolve(thumbnails);
      }
    };
  });
};
