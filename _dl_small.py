import sys
sys.path.insert(0, r'D:\Myfolder\doubao\-')
from toolbox import models
models.set_hf_mirror()
import faster_whisper
m = faster_whisper.WhisperModel('small', device='cpu', compute_type='int8',
                                download_root=str(models.models_dir()))
print('MODEL_DONE:', models.models_dir())
