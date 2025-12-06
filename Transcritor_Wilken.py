"D:\Python - Wilken\Transcritor_Wilken.py""""
Transcritor (Google / Vosk) - correção: evitar logs repetidos do ffmpeg (cache do binário).

Salve como: transcritor_tk_vosk_google_no_ffmpeg_spam.py
Execute: python transcritor_tk_vosk_google_no_ffmpeg_spam.py
"""

import os
import sys
import threading
import tempfile
import queue
import csv
import time
import subprocess
import shutil
import importlib
import urllib.request
import platform
import zipfile
import tarfile
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk
import webbrowser

# Caminho do PNG de erro (referência)
ERROR_IMAGE_PATH = "/mnt/data/e835e590-a6b3-4e4f-82ff-75c48cb4b353.png"

# Pasta local para ffmpeg portátil (opcional)
LOCAL_FFMPEG_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), "ffmpeg_local")
os.makedirs(LOCAL_FFMPEG_DIR, exist_ok=True)

# Pasta padrão onde o usuário pode guardar modelos Vosk
DEFAULT_MODELS_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), "models")
os.makedirs(DEFAULT_MODELS_DIR, exist_ok=True)

# --------------------------
# Globals for caching ffmpeg
# --------------------------
_CACHED_FFMPEG = None
_FFMPEG_FIRST_LOG_DONE = False

# --------------------------
# Helpers: pip install
# --------------------------
def run_pip_install(package):
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        return True, f"{package} instalado com sucesso."
    except Exception as e:
        return False, f"Falha ao instalar {package}: {e}"

def check_module(name):
    try:
        spec = importlib.util.find_spec(name)
        return spec is not None
    except Exception:
        return False

def install_dependencies(callback=lambda s: None):
    PACKAGES = [
        ("pydub", "pydub"),
        ("soundfile", "soundfile"),
        ("numpy", "numpy"),
        ("tqdm", "tqdm"),
        ("SpeechRecognition", "speech_recognition"),
        ("vosk", "vosk")
    ]
    callback("Iniciando verificação/instalação pip...\n")
    for pip_name, import_name in PACKAGES:
        callback(f"Verificando {pip_name} ... ")
        if check_module(import_name):
            callback("já instalado.\n")
        else:
            callback("não encontrado. Instalando...\n")
            ok, msg = run_pip_install(pip_name)
            callback(msg + "\n")
    callback("\nInstalações pip concluídas (ou tentadas).\n")

# --------------------------
# FFmpeg helpers (robusto; tenta localizar ou instruir)
# --------------------------
def find_local_ffmpeg_bin(base_dir):
    for root, dirs, files in os.walk(base_dir):
        for fname in files:
            if fname.lower() in ("ffmpeg.exe", "ffmpeg"):
                return os.path.join(root, fname)
    return None

def try_download_ffmpeg(progress_callback=None):
    """
    (Mantido simples) Não implementamos download automático extenso aqui;
    preferimos instruir o usuário. Retorna (False, msg).
    """
    return False, "Download automático de ffmpeg não ativado nesta versão."

def ensure_ffmpeg_available(progress_callback=None):
    """
    Retorna o caminho do ffmpeg executável ou None.
    Usa cache global para evitar logs repetidos. Somente chama progress_callback
    quando encontra o ffmpeg pela primeira vez ou quando não o encontra.
    """
    global _CACHED_FFMPEG, _FFMPEG_FIRST_LOG_DONE
    if _CACHED_FFMPEG:
        # Já temos um caminho detectado, não logar novamente
        return _CACHED_FFMPEG

    # 1) procurar no PATH
    path = shutil.which("ffmpeg")
    if path:
        _CACHED_FFMPEG = path
        if progress_callback and not _FFMPEG_FIRST_LOG_DONE:
            progress_callback(f"ffmpeg no PATH: {path}\n")
            _FFMPEG_FIRST_LOG_DONE = True
        return _CACHED_FFMPEG

    # 2) procurar versão local extraída
    ff_local = find_local_ffmpeg_bin(LOCAL_FFMPEG_DIR)
    if ff_local:
        _CACHED_FFMPEG = ff_local
        # adicionar ao PATH para subprocess
        os.environ["PATH"] = os.path.dirname(ff_local) + os.pathsep + os.environ.get("PATH","")
        if progress_callback and not _FFMPEG_FIRST_LOG_DONE:
            progress_callback(f"ffmpeg local: {ff_local}\n")
            _FFMPEG_FIRST_LOG_DONE = True
        return _CACHED_FFMPEG

    # 3) não encontrado
    if progress_callback and not _FFMPEG_FIRST_LOG_DONE:
        progress_callback("ffmpeg não encontrado. Instale manualmente (choco/apt/brew) e reinicie.\n")
        _FFMPEG_FIRST_LOG_DONE = True
    return None

# --------------------------
# Conversão via ffmpeg CLI
# --------------------------
def convert_to_wav(src_path, target_path, sample_rate=16000, progress_callback=None):
    ff = ensure_ffmpeg_available(progress_callback=progress_callback)
    if not ff:
        raise RuntimeError("ffmpeg não disponível. Instale ffmpeg (choco/brew/apt) e reinicie.")
    cmd = [ff, "-y", "-i", src_path, "-ar", str(sample_rate), "-ac", "1", "-sample_fmt", "s16", target_path]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        stderr = p.stderr.decode(errors='ignore')
        raise RuntimeError(f"ffmpeg falhou: {stderr}")
    return target_path

def get_duration_seconds(wav_path):
    ff = ensure_ffmpeg_available()
    if not ff:
        raise RuntimeError("ffprobe/ffmpeg não disponível.")
    # tentar ffprobe
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", wav_path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0:
            return float(p.stdout.decode().strip())
    except Exception:
        pass
    # fallback parse ffmpeg -i stderr
    p2 = subprocess.run([ff, "-i", wav_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = p2.stderr.decode(errors='ignore')
    import re
    m = re.search(r"Duration:\s*([\d:.]+)", out)
    if m:
        parts = m.group(1).split(':')
        secs = float(parts[-1]) + 60*int(parts[-2]) + 3600*int(parts[-3])
        return secs
    raise RuntimeError("Não foi possível obter duração do arquivo.")

# --------------------------
# Detectar modelos Vosk na pasta escolhida
# --------------------------
def detect_vosk_models(models_dir):
    out = []
    if not models_dir or not os.path.exists(models_dir):
        return out
    for entry in os.listdir(models_dir):
        full = os.path.join(models_dir, entry)
        if os.path.isdir(full):
            files = set(os.listdir(full))
            if any(x in files for x in ("am", "model", "conf")) or any(f.lower().endswith(".txt") for f in files):
                out.append((entry, full))
            else:
                for sub in os.listdir(full):
                    subp = os.path.join(full, sub)
                    if os.path.isdir(subp):
                        sf = set(os.listdir(subp))
                        if any(x in sf for x in ("am", "model", "conf")):
                            out.append((entry, full))
                            break
    return out

# --------------------------
# Função robusta para tentar carregar modelo Vosk e explicar falhas
# --------------------------
def try_load_vosk_model(model_path):
    if not model_path or not os.path.exists(model_path) or not os.path.isdir(model_path):
        return None, "Caminho do modelo inválido ou inexistente. Verifique se extraiu o ZIP do modelo e apontou para a pasta correta."
    try:
        entries = os.listdir(model_path)
    except Exception as e:
        return None, f"Erro ao listar a pasta do modelo: {e}"
    if len(entries) == 0:
        return None, "A pasta do modelo está vazia. Verifique se extraiu os arquivos do modelo Vosk."
    if not importlib.util.find_spec("vosk"):
        return None, "Pacote 'vosk' não está instalado. Use 'Instalar dependências (pip)'."
    try:
        vosk_mod = importlib.import_module("vosk")
        Model = getattr(vosk_mod, "Model", None)
        if Model is None:
            return None, "Módulo 'vosk' carregado mas não contém 'Model' (versão inesperada)."
        model_obj = Model(model_path)
        return model_obj, None
    except Exception as e:
        msg = (
            f"Falha ao criar o modelo Vosk a partir de '{model_path}': {e}\n\n"
            "Possíveis causas:\n"
            "- Você apontou para o zip do modelo em vez de ter extraído a pasta.\n"
            "- O modelo baixado não é compatível com a versão do pacote vosk instalada.\n"
            "- O pacote 'vosk' não está instalado corretamente.\n\n"
            "Soluções:\n"
            "- Extraia o arquivo ZIP do modelo e selecione a pasta extraída.\n"
            "- Instale/atualize o pacote vosk: python -m pip install -U vosk\n"
            "- Baixe um modelo oficial e compatível em https://alphacephei.com/vosk/models\n"
        )
        return None, msg

# --------------------------
# Transcrição por chunks (Google / Vosk)
# --------------------------
def transcribe_by_chunks(backend, backend_state, wav_path, language, chunk_length_sec, overlap_sec, queue_out, stop_event=None):
    total_sec = get_duration_seconds(wav_path)
    step = max(1, chunk_length_sec - overlap_sec) if chunk_length_sec > overlap_sec else chunk_length_sec
    starts = list(range(0, int(total_sec) + 1, step))
    n_chunks = len(starts) if starts else 1

    # pegar ffmpeg UMA vez aqui (sem progress_callback para evitar logs)
    ff = ensure_ffmpeg_available(progress_callback=None)
    if not ff:
        queue_out.put({'error': 'ffmpeg não disponível para extrair chunks.'})
        queue_out.put({'done': True})
        return

    tempdir = tempfile.mkdtemp(prefix="chunks_")
    try:
        for idx, s in enumerate(starts):
            if stop_event and stop_event.is_set():
                queue_out.put({'status': 'Parada solicitada pelo usuário.'})
                break
            start_s = float(s)
            duration_s = float(chunk_length_sec)
            chunk_path = os.path.join(tempdir, f"chunk_{idx}.wav")
            cmd = [ff, "-y", "-i", wav_path, "-ss", f"{start_s:.3f}", "-t", f"{duration_s:.3f}",
                   "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", chunk_path]
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if p.returncode != 0:
                queue_out.put({'error': f"ffmpeg falhou ao extrair chunk {idx}: {p.stderr.decode(errors='ignore')}"})
                queue_out.put({'progress': (idx+1)/n_chunks})
                continue

            # transcrever chunk via backend
            try:
                if backend == "google":
                    import speech_recognition as sr
                    r = sr.Recognizer()
                    with sr.AudioFile(chunk_path) as source:
                        audio_data = r.record(source)
                    try:
                        text = r.recognize_google(audio_data, language=language or "pt-BR")
                    except sr.UnknownValueError:
                        text = ""
                    except sr.RequestError as e:
                        raise RuntimeError(f"Erro requisição Google Speech API: {e}")
                    queue_out.put({'segment': (start_s, min(start_s + duration_s, start_s + duration_s), text)})
                elif backend == "vosk":
                    model_obj = backend_state.get('model_obj')
                    if model_obj is None:
                        model_path = backend_state.get('model_path')
                        mod_obj, err = try_load_vosk_model(model_path)
                        if err:
                            raise RuntimeError(err)
                        model_obj = mod_obj
                        backend_state['model_obj'] = model_obj
                    from vosk import KaldiRecognizer
                    import wave, json
                    wf = wave.open(chunk_path, "rb")
                    rec = KaldiRecognizer(model_obj, wf.getframerate())
                    rec.SetWords(True)
                    full_text = []
                    while True:
                        data = wf.readframes(4000)
                        if len(data) == 0:
                            break
                        if rec.AcceptWaveform(data):
                            res = rec.Result()
                            try:
                                j = json.loads(res)
                                txt = j.get('text','').strip()
                                if txt:
                                    full_text.append(txt)
                            except Exception:
                                pass
                    final = rec.FinalResult()
                    try:
                        j = json.loads(final)
                        if j.get('text'):
                            full_text.append(j.get('text'))
                    except Exception:
                        pass
                    text = " ".join(full_text).strip()
                    queue_out.put({'segment': (start_s, min(start_s + duration_s, start_s + duration_s), text)})
                else:
                    queue_out.put({'error': f"Backend desconhecido: {backend}"})
                    break
            except Exception as e:
                queue_out.put({'error': f"Erro transcrevendo chunk {idx}: {e}"})

            queue_out.put({'progress': (idx+1)/n_chunks})
    finally:
        try:
            for f in os.listdir(tempdir):
                os.remove(os.path.join(tempdir, f))
            os.rmdir(tempdir)
        except Exception:
            pass
    queue_out.put({'done': True})

# --------------------------
# GUI APP
# --------------------------
class TranscriberApp:
    def __init__(self, root):
        self.root = root
        root.title("Transcritor - Vosk / Google (no ffmpeg spam)")
        # maximizar janela
        try:
            root.state('zoomed')
        except Exception:
            try:
                root.attributes('-zoomed', True)
            except Exception:
                root.geometry("{0}x{1}+0+0".format(root.winfo_screenwidth(), root.winfo_screenheight()))

        # state
        self.audio_path = tk.StringVar()
        self.backend_choice = tk.StringVar(value="google")  # google or vosk
        self.lang_choice = tk.StringVar(value="pt-BR")
        self.models_dir = tk.StringVar(value=DEFAULT_MODELS_DIR)
        self.detected_models = []  # list of (display, fullpath)
        self.selected_model = tk.StringVar(value="")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.is_transcribing = False
        self.segments = []
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.backend_state = {'model_path': None, 'model_obj': None}
        self.model_size_suggestion = tk.StringVar(value="medium")
        self.chunk_len_map = {'tiny':5, 'medium':20, 'larger':60}

        # top controls
        top = ttk.Frame(root, padding=8)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(top, text="Instalar dependências (pip)", command=self.gui_install_dependencies).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Carregar arquivo", command=self.load_file).pack(side=tk.LEFT, padx=6)
        ttk.Label(top, textvariable=self.audio_path, width=60).pack(side=tk.LEFT, padx=8)

        ttk.Label(top, text="Backend:").pack(side=tk.LEFT, padx=(10,2))
        ttk.OptionMenu(top, self.backend_choice, self.backend_choice.get(), "google", "vosk", command=self.on_backend_change).pack(side=tk.LEFT)

        ttk.Label(top, text="Sugestão (tiny/medium/larger):").pack(side=tk.LEFT, padx=(10,2))
        ttk.OptionMenu(top, self.model_size_suggestion, self.model_size_suggestion.get(), "tiny", "medium", "larger", command=self.on_model_suggestion_change).pack(side=tk.LEFT)

        ttk.Label(top, text="Pasta modelos:").pack(side=tk.LEFT, padx=(10,2))
        ttk.Entry(top, textvariable=self.models_dir, width=30).pack(side=tk.LEFT, padx=(0,6))
        ttk.Button(top, text="Escolher pasta para modelos", command=self.choose_models_dir).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Detectar modelos locais", command=self.detect_models_action).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Abrir página Vosk", command=lambda: webbrowser.open("https://alphacephei.com/vosk/models")).pack(side=tk.LEFT, padx=6)

        ttk.Label(top, text="Idioma (ex: pt-BR/en-US):").pack(side=tk.LEFT, padx=(10,2))
        ttk.Entry(top, textvariable=self.lang_choice, width=10).pack(side=tk.LEFT)

        ttk.Button(top, text="Iniciar transcrição", command=self.start_transcription).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Parar", command=self.stop_transcription).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Ver imagem de erro", command=self.show_error_image).pack(side=tk.LEFT, padx=6)

        # progress
        self.progress = ttk.Progressbar(root, orient=tk.HORIZONTAL, length=900, mode='determinate', variable=self.progress_var)
        self.progress.pack(pady=8)

        # main area
        main = ttk.Frame(root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(main)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ttk.Label(left, text="Transcrição (ao vivo):").pack(anchor='w')
        self.text_widget = tk.Text(left, wrap=tk.WORD)
        self.text_widget.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        right = ttk.Frame(main, width=420)
        right.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(right, text="Modelos Vosk detectados:").pack(anchor='w')
        self.model_combo = ttk.Combobox(right, textvariable=self.selected_model, values=[], width=60)
        self.model_combo.pack(fill=tk.X, padx=4, pady=4)
        ttk.Button(right, text="Selecionar modelo (usa seleção acima)", command=self.select_model_from_combo).pack(fill=tk.X, padx=4, pady=2)

        ttk.Label(right, text="Segmentos (lista):").pack(anchor='w')
        self.listbox = tk.Listbox(right, width=70)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        ttk.Button(right, text="Exportar TXT (sem timestamps)", command=lambda: self.export_file(False,'txt')).pack(fill=tk.X, padx=4, pady=2)
        ttk.Button(right, text="Exportar TXT (com timestamps)", command=lambda: self.export_file(True,'txt')).pack(fill=tk.X, padx=4, pady=2)
        ttk.Button(right, text="Exportar CSV (com timestamps)", command=lambda: self.export_file(True,'csv')).pack(fill=tk.X, padx=4, pady=2)

        ttk.Separator(right, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=6)
        ttk.Label(right, text="Configuração de chunk (segundos) — sugerido:").pack(anchor='w')
        self.chunk_len_var = tk.IntVar(value=self.chunk_len_map[self.model_size_suggestion.get()])
        self.overlap_var = tk.IntVar(value=1)
        ttk.Label(right, text="Chunk length").pack(anchor='w')
        ttk.Entry(right, textvariable=self.chunk_len_var, width=6).pack(anchor='w', padx=4)
        ttk.Label(right, text="Overlap").pack(anchor='w')
        ttk.Entry(right, textvariable=self.overlap_var, width=6).pack(anchor='w', padx=4)

        # periodic check
        self.root.after(200, self.check_queue)

        # init UI state and detect models
        self.update_ui_state()
        self.detect_models_action()

    # UI callbacks
    def on_backend_change(self, _=None):
        self.update_ui_state()

    def on_model_suggestion_change(self, _=None):
        s = self.model_size_suggestion.get()
        self.chunk_len_var.set(self.chunk_len_map.get(s, 10))
        self.text_widget.insert(tk.END, f"[INFO] Sugestão de modelo: {s} — chunk ajustado para {self.chunk_len_var.get()}s\n")
        self.text_widget.see(tk.END)

    def update_ui_state(self):
        backend = self.backend_choice.get()
        if backend == "google":
            self.text_widget.insert(tk.END, "[INFO] Backend Google selecionado: não usa modelo local. Escolha idioma e inicie.\n")
            self.text_widget.see(tk.END)
        else:
            self.text_widget.insert(tk.END, "[INFO] Backend Vosk selecionado: detecte e selecione um modelo local.\n")
            self.text_widget.see(tk.END)

    def choose_models_dir(self):
        sel = filedialog.askdirectory(title="Escolha a pasta onde guarda modelos Vosk")
        if sel:
            self.models_dir.set(sel)
            self.text_widget.insert(tk.END, f"[INFO] Pasta de modelos definida: {sel}\n")
            self.text_widget.see(tk.END)
            self.detect_models_action()

    def detect_models_action(self):
        models_dir = self.models_dir.get() or DEFAULT_MODELS_DIR
        detected = detect_vosk_models(models_dir)
        self.detected_models = detected
        if detected:
            vals = [f"{name} -> {path}" for name, path in detected]
            self.model_combo['values'] = vals
            self.model_combo.current(0)
            self.selected_model.set(vals[0])
            self.text_widget.insert(tk.END, f"[INFO] {len(detected)} modelo(s) detectado(s) em {models_dir}\n")
        else:
            self.model_combo['values'] = []
            self.selected_model.set("")
            self.text_widget.insert(tk.END, f"[INFO] Nenhum modelo Vosk detectado em {models_dir}. Baixe em https://alphacephei.com/vosk/models e extraia.\n")
        self.text_widget.see(tk.END)

    def select_model_from_combo(self):
        sel = self.model_combo.get()
        if not sel:
            messagebox.showwarning("Selecionar modelo", "Escolha um modelo na lista primeiro.")
            return
        for name, path in self.detected_models:
            display = f"{name} -> {path}"
            if display == sel:
                self.text_widget.insert(tk.END, f"[INFO] Tentando carregar modelo: {path}\n")
                self.text_widget.see(tk.END)
                model_obj, err = try_load_vosk_model(path)
                if model_obj:
                    self.backend_state['model_path'] = path
                    self.backend_state['model_obj'] = model_obj
                    self.text_widget.insert(tk.END, "[OK] Modelo carregado com sucesso e selecionado para uso.\n")
                    self.text_widget.see(tk.END)
                else:
                    self.backend_state['model_path'] = path
                    self.backend_state['model_obj'] = None
                    self.text_widget.insert(tk.END, f"[ERRO] {err}\n")
                    self.text_widget.insert(tk.END, "Verifique se você extraiu o ZIP do modelo e se o 'vosk' está instalado.\n")
                    self.text_widget.see(tk.END)
                    messagebox.showerror("Erro carregando modelo Vosk", err)
                return
        messagebox.showwarning("Selecionar modelo", "Modelo não encontrado na lista (talvez a pasta tenha sido movida). Detecte novamente.")

    def gui_install_dependencies(self):
        def write(msg):
            self.text_widget.insert(tk.END, msg)
            self.text_widget.see(tk.END)
        def worker():
            install_dependencies(write)
            write("\nInstalação concluída. Se for usar Vosk, baixe um modelo em https://alphacephei.com/vosk/models e extraia na pasta de modelos.\n")
        threading.Thread(target=worker, daemon=True).start()

    def load_file(self):
        path = filedialog.askopenfilename(filetypes=[("Áudio e Vídeo", "*.mp3 *.wav *.flac *.mp4 *.m4a *.aac *.ogg"), ("Todos", "*.*")])
        if path:
            self.audio_path.set(path)

    def show_error_image(self):
        path = ERROR_IMAGE_PATH
        if os.path.exists(path):
            try:
                if sys.platform.startswith("win"):
                    os.startfile(path)
                elif sys.platform.startswith("darwin"):
                    subprocess.run(["open", path])
                else:
                    subprocess.run(["xdg-open", path])
                return
            except Exception:
                pass
        messagebox.showinfo("Imagem de erro (caminho)", f"Arquivo: {path}\n(ou não encontrado no sistema)")

    def start_transcription(self):
        if self.is_transcribing:
            messagebox.showinfo("Já em andamento", "Uma transcrição já está em andamento.")
            return
        path = self.audio_path.get()
        if not path or not os.path.exists(path):
            messagebox.showerror("Erro", "Escolha um arquivo de áudio válido primeiro.")
            return

        tmp_wav = os.path.join(tempfile.gettempdir(), f"transcribe_in_{int(time.time())}.wav")
        try:
            # chamar ensure_ffmpeg_available uma vez com callback para mostrar mensagem inicial (se necessário)
            ensure_ffmpeg_available(progress_callback=lambda m: self.queue.put({'status': m}))
            self.text_widget.insert(tk.END, "Convertendo arquivo para WAV (ffmpeg)...\n")
            self.text_widget.see(tk.END)
            convert_to_wav(path, tmp_wav, sample_rate=16000, progress_callback=lambda m: None)
        except Exception as e:
            messagebox.showerror("Erro na conversão", f"Não foi possível converter o arquivo: {e}")
            return

        backend = self.backend_choice.get()
        language = self.lang_choice.get().strip() or None
        chunk_len = max(1, int(self.chunk_len_var.get()))
        overlap = max(0, int(self.overlap_var.get()))

        if backend == "vosk":
            model_path = self.backend_state.get('model_path')
            model_obj = self.backend_state.get('model_obj')
            if model_obj is None:
                self.text_widget.insert(tk.END, "[INFO] Modelo Vosk não estava carregado — tentando carregar agora...\n")
                self.text_widget.see(tk.END)
                model_obj, err = try_load_vosk_model(model_path)
                if err:
                    messagebox.showerror("Erro no modelo Vosk", err)
                    self.text_widget.insert(tk.END, f"[ERRO] {err}\n")
                    self.text_widget.see(tk.END)
                    return
                self.backend_state['model_obj'] = model_obj
                self.text_widget.insert(tk.END, "[OK] Modelo Vosk carregado com sucesso.\n")
                self.text_widget.see(tk.END)

        def worker():
            self.is_transcribing = True
            self.stop_event.clear()
            self.segments = []
            self.queue.put({'status': f"Iniciando backend {backend}..."})
            try:
                transcribe_by_chunks(backend, self.backend_state, tmp_wav, language, chunk_len, overlap, self.queue, stop_event=self.stop_event)
            except Exception as e:
                self.queue.put({'error': f"Erro na transcrição: {e}"})
            finally:
                self.is_transcribing = False

        self.text_widget.delete("1.0", tk.END)
        self.listbox.delete(0, tk.END)
        self.progress_var.set(0.0)
        threading.Thread(target=worker, daemon=True).start()

    def stop_transcription(self):
        if self.is_transcribing:
            self.stop_event.set()
            self.text_widget.insert(tk.END, "[Aguardando parada...]\n")
            self.text_widget.see(tk.END)
        else:
            messagebox.showinfo("Parar", "Não há transcrição em andamento.")

    def check_queue(self):
        while True:
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break
            if 'status' in msg:
                self.text_widget.insert(tk.END, f"[STATUS] {msg['status']}\n")
                self.text_widget.see(tk.END)
            if 'error' in msg:
                messagebox.showerror("Erro", msg['error'])
                self.text_widget.insert(tk.END, f"[ERRO] {msg['error']}\n")
                self.text_widget.see(tk.END)
            if 'progress' in msg:
                pct = float(msg['progress']) * 100
                self.progress_var.set(pct)
            if 'segment' in msg:
                s, e, t = msg['segment']
                self.segments.append((s, e, t))
                ts = f"[{s:.2f}s - {e:.2f}s] {t}\n"
                self.text_widget.insert(tk.END, ts)
                self.text_widget.see(tk.END)
                self.listbox.insert(tk.END, f"{s:.2f}-{e:.2f}: {t[:80]}...")
            if 'done' in msg:
                self.progress_var.set(100.0)
                self.text_widget.insert(tk.END, "[Transcrição concluída]\n")
                self.text_widget.see(tk.END)
        self.root.after(200, self.check_queue)

    def export_file(self, with_timestamps=False, fmt='txt'):
        if not self.segments:
            messagebox.showwarning("Nada", "Nenhum segmento transcrito ainda.")
            return
        out = filedialog.asksaveasfilename(defaultextension=f".{fmt}", filetypes=[(fmt.upper(), f"*.{fmt}")])
        if not out:
            return
        try:
            if fmt == 'txt':
                with open(out, 'w', encoding='utf-8') as f:
                    if with_timestamps:
                        for s,e,t in self.segments:
                            f.write(f"[{s:.2f}-{e:.2f}] {t}\n")
                    else:
                        for s,e,t in self.segments:
                            f.write(t + "\n")
            elif fmt == 'csv':
                with open(out, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(["start","end","text"])
                    for s,e,t in self.segments:
                        writer.writerow([f"{s:.2f}", f"{e:.2f}", t])
            messagebox.showinfo("Exportado", f"Arquivo salvo em: {out}")
        except Exception as e:
            messagebox.showerror("Erro ao exportar", str(e))

# Run
if __name__ == "__main__":
    root = tk.Tk()
    app = TranscriberApp(root)
    root.mainloop()
