import argparse
import os
import sys

import qdarktheme
import torch
from PySide6.QtCore import QEvent, QSettings
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import configuration
from . import font_awesome as fa
from .main_window import MainWindow
from .image_metadata import (
    ImageMetadata,
    Img2ImgMetadata,
    ControlNetMetadata,
    UpscaleMetadata,
    FaceRestorationMetadata,
    HighResMetadata,
)


class Application(QApplication):
    settings: QSettings = None
    collections: list[str] = []

    def __init__(self, argv):
        if sys.platform == "darwin":
            from Foundation import NSBundle

            bundle = NSBundle.mainBundle()
            info_dict = bundle.localizedInfoDictionary() or bundle.infoDictionary()
            info_dict["CFBundleName"] = configuration.APP_NAME

        super().__init__(argv)

        parser = argparse.ArgumentParser(description=configuration.APP_NAME)
        parser.add_argument("--root")
        args = parser.parse_args(argv[1:])

        configuration.set_resources_path(os.path.join(os.getcwd(), "seed_alchemy/resources"))
        if args.root:
            os.chdir(os.path.expanduser(args.root))

        dpi = QApplication.primaryScreen().logicalDotsPerInch()
        configuration.font_scale_factor = 96 / dpi

        # Directories
        os.makedirs(configuration.IMAGES_PATH, exist_ok=True)
        os.makedirs(configuration.MODELS_PATH, exist_ok=True)

        # Settings
        image_meta = ImageMetadata()
        img2img_meta = Img2ImgMetadata()
        control_net_meta = ControlNetMetadata()
        upscale_meta = UpscaleMetadata()
        face_meta = FaceRestorationMetadata()
        high_res_meta = HighResMetadata()

        self.settings = QSettings("settings.ini", QSettings.IniFormat)
        self.set_default_setting("local_models_path", "")
        self.set_default_setting("reduce_memory", True)
        self.set_default_setting("safety_checker", True)
        self.set_default_setting("float32", not torch.cuda.is_available())
        self.set_default_setting("collection", "outputs")
        self.set_default_setting("type", "image")
        self.set_default_setting("scheduler", image_meta.scheduler)
        self.set_default_setting("model", image_meta.model)
        self.set_default_setting("prompt", image_meta.prompt)
        self.set_default_setting("negative_prompt", image_meta.negative_prompt)
        self.set_default_setting("manual_seed", False)
        self.set_default_setting("seed", image_meta.seed)
        self.set_default_setting("num_images_per_prompt", 1)
        self.set_default_setting("num_inference_steps", image_meta.num_inference_steps)
        self.set_default_setting("guidance_scale", image_meta.guidance_scale)
        self.set_default_setting("width", image_meta.width)
        self.set_default_setting("height", image_meta.height)
        self.set_default_setting("img2img_enabled", False)
        self.set_default_setting("img2img_source", img2img_meta.source)
        self.set_default_setting("img2img_noise", img2img_meta.noise)
        self.set_default_setting("control_net_enabled", False)
        self.set_default_setting("control_net_guidance_start", control_net_meta.guidance_start)
        self.set_default_setting("control_net_guidance_end", control_net_meta.guidance_end)
        self.set_default_setting("control_net_conditions", "[]")
        self.set_default_setting("upscale_enabled", False)
        self.set_default_setting("upscale_factor", upscale_meta.factor)
        self.set_default_setting("upscale_denoising", upscale_meta.denoising)
        self.set_default_setting("upscale_blend", upscale_meta.blend)
        self.set_default_setting("face_enabled", False)
        self.set_default_setting("face_blend", face_meta.blend)
        self.set_default_setting("high_res_enabled", False)
        self.set_default_setting("high_res_factor", high_res_meta.factor)
        self.set_default_setting("high_res_guidance_scale", high_res_meta.guidance_scale)
        self.set_default_setting("high_res_noise", high_res_meta.noise)
        self.set_default_setting("high_res_steps", high_res_meta.steps)

        self.set_default_setting("install_control_net_v10", "False")
        self.set_default_setting("install_control_net_v11", "True")
        self.set_default_setting("install_control_net_mediapipe_v2", "True")
        self.set_default_setting("huggingface_models", ["runwayml/stable-diffusion-v1-5"])

        configuration.load_from_settings(self.settings)

        # Collections
        self.collections = sorted(
            [
                entry
                for entry in os.listdir(configuration.IMAGES_PATH)
                if os.path.isdir(os.path.join(configuration.IMAGES_PATH, entry))
            ]
        )
        if not self.collections:
            os.makedirs(os.path.join(configuration.IMAGES_PATH, "outputs"))
            self.collections = ["outputs"]

        # QT configuration
        self.setWindowIcon(QIcon(configuration.get_resource_path("app_icon.png")))
        self.setApplicationName(configuration.APP_NAME)
        qdarktheme.setup_theme("auto", corner_shape="sharp", additional_qss="QToolTip { border: 0px; }")
        fa.load()

        # Main window
        self.main_window = MainWindow(self.settings, self.collections)
        self.main_window.show()

    def set_default_setting(self, key: str, value):
        if not self.settings.contains(key):
            self.settings.setValue(key, value)

    def event(self, event):
        if event.type() == QEvent.Quit:
            if self.main_window.hide_if_thread_running():
                return False
        return super().event(event)
