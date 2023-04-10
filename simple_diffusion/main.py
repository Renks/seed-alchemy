import os

os.environ['DISABLE_TELEMETRY'] = '1'
os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] ='1'

import gc
import random
import re
import shutil
import sys
import traceback

import torch
from compel import Compel
from PIL import Image, PngImagePlugin
from PySide6.QtCore import (QEvent, QPoint, QRect, QSettings, QSize, Qt,
                            QThread, Signal)
from PySide6.QtGui import (QAction, QColor, QFontMetrics, QIcon, QImage,
                           QPainter, QPalette, QPen, QPixmap, QTextCharFormat,
                           QTextCursor)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup,
                               QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMenuBar, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QSlider, QSpinBox, QSplitter,
                               QStyle, QStyledItemDelegate, QStyleOptionSlider,
                               QTableWidget, QTableWidgetItem, QTextEdit,
                               QToolBar, QVBoxLayout, QWidget)
from spellchecker import SpellChecker

if sys.platform == 'darwin':
    from AppKit import NSApplication
    from Foundation import NSBundle

import actions
import configuration
import utils
from configuration import ControlNetCondition, Img2ImgCondition
from image_metadata import ImageMetadata
from pipelines import (ControlNetPipeline, GenerateRequest, Img2ImgPipeline,
                       PipelineCache, Txt2ImgPipeline)
from processors import ESRGANProcessor, GFPGANProcessor, ProcessorBase
from utils import Timer

# -------------------------------------------------------------------------------------------------

generate_preprocessor: ProcessorBase = None
preview_preprocessor: ProcessorBase = None
pipeline_cache: PipelineCache = PipelineCache()
settings: QSettings = None
collections: list[str] = []

def set_default_setting(key: str, value):
    if not settings.contains(key):
        settings.setValue(key, value)

def bool_setting(key: str):
    value = settings.value(key)
    if value is not None:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() == 'true'
    return False

def pil_to_qimage(pil_image: Image.Image):
    data = pil_image.convert('RGBA').tobytes('raw', 'RGBA')
    qimage = QImage(data, pil_image.width, pil_image.height, QImage.Format_RGBA8888)
    return qimage

def latents_to_pil(latents: torch.FloatTensor):
    # Code from InvokeAI
    # https://github.com/invoke-ai/InvokeAI/blob/a1cd4834d127641a865438e668c5c7f050e83587/invokeai/backend/generator/base.py#L502

    # originally adapted from code by @erucipe and @keturn here:
    # https://discuss.huggingface.co/t/decoding-latents-to-rgb-without-upscaling/23204/7

    # these updated numbers for v1.5 are from @torridgristle
    v1_5_latent_rgb_factors = torch.tensor(
        [
            #    R        G        B
            [ 0.3444,  0.1385,  0.0670],  # L1
            [ 0.1247,  0.4027,  0.1494],  # L2
            [-0.3192,  0.2513,  0.2103],  # L3
            [-0.1307, -0.1874, -0.7445],  # L4
        ],
        dtype=latents.dtype,
        device=latents.device,
    )

    latent_image = latents[0].permute(1, 2, 0) @ v1_5_latent_rgb_factors
    latents_ubyte = (
        ((latent_image + 1) / 2)
        .clamp(0, 1)  # change scale from -1..1 to 0..1
        .mul(0xFF)  # to 0..255
        .byte()
    ).cpu()

    return Image.fromarray(latents_ubyte.numpy())

def create_frame_separator():
    separator = QFrame()
    separator.setFrameShape(QFrame.HLine)
    separator.setFrameShadow(QFrame.Sunken)
    palette = separator.palette()
    color = palette.color(QPalette.ColorRole.Mid)
    separator.setStyleSheet(f"QFrame {{ border: 1px solid {color.name()}; }}")
    return separator

class PromptTextEdit(QPlainTextEdit):
    return_pressed = Signal()

    def __init__(self, desired_lines, placeholder_text, parent=None):
        super().__init__(parent)
        self.spell_checker = SpellChecker()
        self.word_pattern = re.compile(r'\b\w+\b')

        font = self.font()
        font.setPointSize(14)
        self.setFont(font)

        font_metrics = QFontMetrics(font)
        line_height = font_metrics.lineSpacing()
        margins = self.contentsMargins()
        frame_width = self.frameWidth()
        document_margins = self.document().documentMargin()

        self.setFixedHeight(line_height * desired_lines + margins.top() + margins.bottom() + 2 * frame_width + 2 * document_margins)
        self.setPlaceholderText(placeholder_text)
        self.setTabChangesFocus(True)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Enter, Qt.Key_Return):
            self.clearFocus()
            self.return_pressed.emit()
        elif key == Qt.Key_Escape:
            self.clearFocus()
        else:
            super().keyPressEvent(event)
            self.highlight_misspelled_words()

        if key in (Qt.Key_Left, Qt.Key_Right):
            event.accept()

    def setPlainText(self, str):
        super().setPlainText(str)
        self.highlight_misspelled_words()

    def highlight_misspelled_words(self):
        text = self.toPlainText()
        words = self.word_pattern.finditer(text)

        extra_selections = []
        for match in words:
            word = match.group()
            if not self.spell_checker.known([word]):
                format = QTextCharFormat()
                format.setUnderlineColor(QColor(Qt.red))
                format.setUnderlineStyle(QTextCharFormat.SingleUnderline)

                index = text.index(word)
                cursor = QTextCursor(self.document())
                cursor.setPosition(index, QTextCursor.MoveAnchor)
                cursor.setPosition(index + len(word), QTextCursor.KeepAnchor)

                extra_selection = QTextEdit.ExtraSelection()
                extra_selection.cursor = cursor
                extra_selection.format = format
                extra_selections.append(extra_selection)

        self.setExtraSelections(extra_selections)

class ThumbnailSelectionDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)

        if option.state & QStyle.State_Selected:
            rect = QRect(option.rect)
            checkmark_start = QPoint(rect.left() * 0.70 + rect.right() * 0.30, rect.top() * 0.50 + rect.bottom() * 0.50)
            checkmark_middle = QPoint(rect.left() * 0.55 + rect.right() * 0.45, rect.top() * 0.35 + rect.bottom() * 0.65)
            checkmark_end = QPoint(rect.left() * 0.25 + rect.right() * 0.75, rect.top() * 0.65 + rect.bottom() * 0.35)

            painter.save()
            painter.setPen(QPen(QColor(0, 255, 0), 8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setRenderHint(QPainter.Antialiasing)
            painter.drawLine(checkmark_start, checkmark_middle)
            painter.drawLine(checkmark_middle, checkmark_end)
            painter.restore()

class ThumbnailListWidget(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.min_thumbnail_size = 100
        self.max_thumbnail_size = 250
        self.spacing = 8

        self.setViewMode(QListWidget.IconMode)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setSpacing(self.spacing)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setItemDelegate(ThumbnailSelectionDelegate(self))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_icon_size()

    def visualRect(self, index):
        rect = super().visualRect(index)
        rect.setWidth(self.iconSize().width())
        rect.setHeight(self.iconSize().height())
        return rect

    def update_icon_size(self):
        style = QApplication.instance().style()
        scrollbar_width = style.pixelMetric(QStyle.PM_ScrollBarExtent, QStyleOptionSlider())
        available_width = self.width() - scrollbar_width - 4
        num_columns = int((available_width) / (self.min_thumbnail_size + self.spacing * 2))
        num_columns = max(1, num_columns)
        new_icon_size = int((available_width - num_columns * self.spacing * 2) / num_columns)
        new_icon_size = max(self.min_thumbnail_size, min(new_icon_size, self.max_thumbnail_size))

        self.setIconSize(QSize(new_icon_size, new_icon_size))

class ThumbnailViewer(QWidget):
    file_dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setAcceptDrops(True)

        self.action_send_to_img2img = actions.send_to_img2img.create()
        self.action_use_prompt = actions.use_prompt.create()
        self.action_use_seed = actions.use_seed.create()
        self.action_use_all = actions.use_all.create()
        self.action_use_initial_image = actions.use_initial_image.create()
        self.action_delete = actions.delete_image.create()
        self.action_reveal_in_finder = actions.reveal_in_finder.create()

        self.collection_combobox = QComboBox()

        self.list_widget = ThumbnailListWidget()
        self.list_widget.customContextMenuRequested.connect(self.show_context_menu)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll_area.setWidget(self.list_widget)

        thumbnail_layout = QVBoxLayout(self)
        thumbnail_layout.setContentsMargins(0, 0, 0, 0)
        thumbnail_layout.addWidget(self.collection_combobox)
        thumbnail_layout.addWidget(scroll_area)

        self.menu = QMenu()
        self.menu.addAction(self.action_send_to_img2img)
        self.menu.addSeparator()
        self.menu.addAction(self.action_use_prompt)
        self.menu.addAction(self.action_use_seed)
        self.menu.addAction(self.action_use_initial_image)
        self.menu.addAction(self.action_use_all)
        self.menu.addSeparator()
        self.menu.addAction(self.action_delete)
        self.menu.addSeparator()
        self.menu.addAction(self.action_reveal_in_finder)

        # Gather collections
        self.collection_combobox.addItems(collections)
        self.collection_combobox.setCurrentText(settings.value('collection'))
        self.collection_combobox.currentIndexChanged.connect(self.update_collection)
        self.update_collection()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet('QListWidget {background-color: #222233;}')

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.setStyleSheet('QListWidget {background-color: black;}')
        pass

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        for url in urls:
            self.file_dropped.emit(url.toLocalFile())
        self.setStyleSheet('QListWidget {background-color: black;}')

        event.acceptProposedAction()

    def collection(self):
        return self.collection_combobox.currentText()

    def update_collection(self):
        collection = self.collection()

        os.makedirs(os.path.join(configuration.THUMBNAILS_PATH, collection), exist_ok=True)
        image_files = sorted([file for file in os.listdir(os.path.join(configuration.IMAGES_PATH, collection)) if file.lower().endswith(('.webp', '.png', '.jpg', '.jpeg', '.gif', '.bmp'))])

        self.list_widget.clear()
        for image_file in image_files:
            image_path = os.path.join(collection, image_file)
            self.add_image(image_path)

        self.list_widget.setCurrentRow(0)

    def select_image(self, rel_path):
        collection = os.path.dirname(rel_path)
        collection_path = os.path.join(configuration.IMAGES_PATH, rel_path)
        if os.path.exists(collection_path):
            self.collection_combobox.setCurrentText(collection)

            for index in range(self.list_widget.count()):
                item = self.list_widget.item(index)
                if item.data(Qt.UserRole) == rel_path:
                    self.list_widget.setCurrentItem(item)
                    break

    def add_image(self, rel_path):
        thumbnail_path = os.path.join(configuration.THUMBNAILS_PATH, rel_path)
        if not os.path.exists(thumbnail_path):
            image_path = os.path.join(configuration.IMAGES_PATH, rel_path)
            with Image.open(image_path) as image:
                thumbnail_image = utils.create_thumbnail(image)
                thumbnail_image.save(thumbnail_path, 'WEBP')

        with Image.open(thumbnail_path) as image:
            pixmap = QPixmap.fromImage(pil_to_qimage(image))
            icon = QIcon(pixmap)
            item = QListWidgetItem()
            item.setIcon(icon)
            item.setData(Qt.UserRole, rel_path)
            self.list_widget.insertItem(0, item)

    def remove_image(self, rel_path):
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            if item.data(Qt.UserRole) == rel_path:
                self.list_widget.takeItem(index)
                break

    def previous_image(self):
        next_row = self.list_widget.currentRow() - 1
        if next_row >= 0:
            self.list_widget.setCurrentRow(next_row)

    def next_image(self):
        next_row = self.list_widget.currentRow() + 1
        if next_row < self.list_widget.count():
            self.list_widget.setCurrentRow(next_row)

    def get_current_metadata(self):
        selected_items = self.list_widget.selectedItems()
        if selected_items:
            item = selected_items[0]
            rel_path = item.data(Qt.UserRole)
            full_path = os.path.join(configuration.IMAGES_PATH, rel_path)
            with Image.open(full_path) as image:
                metadata = ImageMetadata()
                metadata.path = rel_path
                metadata.load_from_image_info(image.info)
                return metadata
        return None

    def show_context_menu(self, point):
        self.menu.exec(self.mapToGlobal(point))

class MetadataRow:
    def __init__(self, label_text, multiline=False):
        self.label = QLabel(label_text)
        self.label.setStyleSheet('font-weight: bold; background-color: transparent')

        self.value = QLabel()
        self.value.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.value.setStyleSheet('background-color: transparent')
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.value.setCursor(Qt.IBeamCursor)

        if multiline:
            self.value.setWordWrap(True)

        self.frame = QFrame()
        self.frame.setContentsMargins(0, 0, 0, 0)
        self.frame.setStyleSheet('background-color: transparent')

        hlayout = QHBoxLayout(self.frame)
        hlayout.setContentsMargins(0, 0, 0, 0)
        hlayout.addWidget(self.label)
        hlayout.addWidget(self.value)

class ImageMetadataFrame(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setStyleSheet('background-color: rgba(0, 0, 0, 127);')

        self.path = MetadataRow('Path:')
        self.type = MetadataRow('Type:')
        self.model = MetadataRow('Model:')
        self.scheduler = MetadataRow('Scheduler:')
        self.prompt = MetadataRow('Prompt:', multiline=True)
        self.negative_prompt = MetadataRow('Negative Prompt:', multiline=True)
        self.seed = MetadataRow('Seed:')
        self.num_inference_steps = MetadataRow('Steps:')
        self.guidance_scale = MetadataRow('CFG Scale:')
        self.size = MetadataRow('Size:')
        self.condition = MetadataRow('Condition:')
        self.control_net = MetadataRow('Control Net:')
        self.source_path = MetadataRow('Source Path:')
        self.img_strength = MetadataRow('Image Strength:')
        self.upscale = MetadataRow('Upscaling:')
        self.face = MetadataRow('Face Restoration:')

        vlayout = QVBoxLayout(self)
        vlayout.addWidget(self.path.frame)
        vlayout.addWidget(self.type.frame)
        vlayout.addWidget(self.scheduler.frame)
        vlayout.addWidget(self.model.frame)
        vlayout.addWidget(self.prompt.frame)
        vlayout.addWidget(self.negative_prompt.frame)
        vlayout.addWidget(self.seed.frame)
        vlayout.addWidget(self.num_inference_steps.frame)
        vlayout.addWidget(self.guidance_scale.frame)
        vlayout.addWidget(self.size.frame)
        vlayout.addWidget(self.condition.frame)
        vlayout.addWidget(self.control_net.frame)
        vlayout.addWidget(self.source_path.frame)
        vlayout.addWidget(self.img_strength.frame)
        vlayout.addWidget(self.upscale.frame)
        vlayout.addWidget(self.face.frame)
        vlayout.addStretch()

    def update(self, metadata):
        self.path.value.setText(metadata.path)
        self.type.value.setText(metadata.type)
        self.scheduler.value.setText(metadata.scheduler)
        self.model.value.setText(metadata.model)
        self.prompt.value.setText(metadata.prompt)
        self.negative_prompt.value.setText(metadata.negative_prompt)
        self.seed.value.setText(str(metadata.seed))
        self.num_inference_steps.value.setText(str(metadata.num_inference_steps))
        self.guidance_scale.value.setText(str(metadata.guidance_scale))
        self.size.value.setText('{:d}x{:d}'.format(metadata.width, metadata.height))
        self.condition.value.setText(metadata.condition)
        if metadata.type == 'img2img':
            condition = configuration.conditions.get(metadata.condition, None)
            if isinstance(condition, ControlNetCondition):
                self.img_strength.frame.setVisible(False)
                self.control_net.frame.setVisible(True)
                self.control_net.value.setText('Preprocess={:s}, Scale={:g}, {:s}'.format(
                    str(metadata.control_net_preprocess),
                    metadata.control_net_scale,
                    metadata.control_net_model
                ))
            else:
                self.control_net.frame.setVisible(False)
                self.img_strength.frame.setVisible(True)
                self.img_strength.value.setText(str(metadata.img_strength))
            self.source_path.frame.setVisible(True)
            self.source_path.value.setText(metadata.source_path)
        else:
            self.control_net.frame.setVisible(False)
            self.source_path.frame.setVisible(False)
            self.img_strength.frame.setVisible(False)
        if metadata.upscale_enabled:
            self.upscale.frame.setVisible(True)
            self.upscale.value.setText('{:d}x, Denoising={:g}, Blend={:g}'.format(
                metadata.upscale_factor,
                metadata.upscale_denoising_strength,
                metadata.upscale_blend_strength
            ))
        else:
            self.upscale.frame.setVisible(False)
        if metadata.face_enabled:
            self.face.frame.setVisible(True)
            self.face.value.setText('Blend={:g}'.format(
                metadata.face_blend_strength
            ))
        else:
            self.face.frame.setVisible(False)

class ImageViewer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setMinimumWidth(300)
        self.padding = 5
        self.minimum_image_size = 100
        self.both_images_visible = False
        self.show_preview = True

        self.left_image_path_ = ''
        self.right_image_path_ = ''

        self.left_image = None
        self.right_image = None
        self.preview_image = None

        self.left_label = QLabel(self)
        self.right_label = QLabel(self)
        self.left_controls_frame = QFrame(self)
        self.right_controls_frame = QFrame(self)
        self.metadata_frame = ImageMetadataFrame(self)
        self.metadata_frame.setVisible(False)

        self.locate_source_button = actions.locate_source.tool_button()
        self.send_to_img2img_button = actions.send_to_img2img.tool_button()
        self.use_prompt_button = actions.use_prompt.tool_button()
        self.use_seed_button = actions.use_seed.tool_button()
        self.use_initial_image_button = actions.use_initial_image.tool_button()
        self.use_all_button = actions.use_all.tool_button()
        self.toggle_metadata_button = actions.toggle_metadata.tool_button()
        self.toggle_metadata_button.toggled.connect(self.on_metadata_button_changed)
        self.toggle_preview_button = actions.toggle_preview.tool_button()
        self.toggle_preview_button.setChecked(True)
        self.toggle_preview_button.toggled.connect(self.on_preview_button_changed)
        self.delete_button = actions.delete_image.tool_button()

        left_controls_layout = QHBoxLayout(self.left_controls_frame)
        left_controls_layout.setContentsMargins(0, 0, 0, 0)
        left_controls_layout.setSpacing(0)
        left_controls_layout.addStretch()
        left_controls_layout.addWidget(self.locate_source_button)
        left_controls_layout.addStretch()

        right_controls_layout = QHBoxLayout(self.right_controls_frame)
        right_controls_layout.setContentsMargins(0, 0, 0, 0)
        right_controls_layout.setSpacing(0)
        right_controls_layout.addStretch()
        right_controls_layout.addWidget(self.send_to_img2img_button)
        right_controls_layout.addSpacing(8)
        right_controls_layout.addWidget(self.use_prompt_button)
        right_controls_layout.addWidget(self.use_seed_button)
        right_controls_layout.addWidget(self.use_initial_image_button)
        right_controls_layout.addWidget(self.use_all_button)
        right_controls_layout.addSpacing(8)
        right_controls_layout.addWidget(self.toggle_metadata_button)
        right_controls_layout.addWidget(self.toggle_preview_button)
        right_controls_layout.addSpacing(8)
        right_controls_layout.addWidget(self.delete_button)
        right_controls_layout.addStretch()

        background_color = QApplication.instance().palette().color(QPalette.Base)
        self.setStyleSheet(f'ImageViewer {{ background-color: {background_color.name()}; }}')
        self.setAttribute(Qt.WA_StyledBackground, True)

    def resizeEvent(self, event):
        self.update_images()

    def update_images(self):
        widget_width = self.width()
        widget_height = self.height()
        controls_height = 24

        use_preview_image = self.preview_image is not None and self.show_preview
        right_scale_factor = 1 if use_preview_image else self.metadata.upscale_factor if self.right_image else 1
        left_scale_factor = self.left_metadata.upscale_factor if self.left_image else 1
        right_image = self.preview_image if use_preview_image else self.right_image

        right_image_width = right_image.width() / right_scale_factor if right_image is not None else 1
        right_image_height = right_image.height() / right_scale_factor if right_image is not None else 1
        left_image_width = self.left_image.width() / left_scale_factor if self.left_image is not None else right_image_width
        left_image_height = self.left_image.height() / left_scale_factor if self.left_image is not None else right_image_height

        left_min_size = left_image_height // 2
        right_min_size = right_image_height // 2

        if self.both_images_visible:
            available_height = widget_height - controls_height - 4 * self.padding
            available_width = widget_width - 3 * self.padding

            right_height = min(available_height, right_image_height)
            right_width = int(right_image_width * (right_height / right_image_height))

            remaining_width = available_width - right_width
            left_width = min(remaining_width, left_image_width)
            left_height = int(left_image_height * (left_width / left_image_width))

            if left_height > available_height:
                left_height = available_height
                left_width = int(left_image_width * (left_height / left_image_height))

            if left_height < left_min_size:
                left_height = left_min_size
                left_width = int(left_image_width * (left_height / left_image_height))
                right_width = available_width - left_width
                right_height = int(right_image_height * (right_width / right_image_width))
                if right_height > available_height:
                    right_height = available_height
                    right_width = int(right_image_width * (right_height / right_image_height))
                if right_height < right_min_size:
                    right_height = right_min_size
                    right_width = int(right_image_width * (right_height / right_image_height))

            if self.left_image is not None:
                left_pixmap = QPixmap.fromImage(self.left_image).scaled(left_width, left_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.left_label.setPixmap(left_pixmap)
                self.left_label.setStyleSheet('')  
            else:
                self.left_label.setText('Choose a Source Image')
                self.left_label.setStyleSheet('border: 2px solid white;')
                self.left_label.setWordWrap(True)
                self.left_label.setAlignment(Qt.AlignCenter)

            if right_image is not None:
                right_pixmap = QPixmap.fromImage(right_image).scaled(right_width, right_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.right_label.setPixmap(right_pixmap)

            left_x = (widget_width - left_width - right_width - self.padding) // 2
            left_y = controls_height + 2 * self.padding + (available_height - left_height) // 2
            right_x = left_x + self.padding + left_width
            right_y = controls_height + 2 * self.padding + (available_height - right_height) // 2

            self.left_controls_frame.setVisible(True)
            self.left_label.setVisible(True)

            self.left_controls_frame.setGeometry(left_x, self.padding, left_width, controls_height)
            self.left_label.setGeometry(left_x, left_y, left_width, left_height)
            self.right_controls_frame.setGeometry(right_x, self.padding, right_width, controls_height)
            self.right_label.setGeometry(right_x, right_y, right_width, right_height)
            self.metadata_frame.setGeometry(right_x, right_y, right_width, right_height)
        else:
            available_height = widget_height - controls_height - 4 * self.padding
            available_width = widget_width - 2 * self.padding

            right_height = min(available_height, right_image_height)
            right_width = int(right_image_width * (right_height / right_image_height))

            if right_image is not None:
                right_pixmap = QPixmap.fromImage(right_image).scaled(right_width, right_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.right_label.setPixmap(right_pixmap)

            right_x = (widget_width - right_width) // 2
            right_y = controls_height + 2 * self.padding + (available_height - right_height) // 2

            self.left_controls_frame.setVisible(False)
            self.left_label.setVisible(False)

            self.right_controls_frame.setGeometry(right_x, self.padding, right_width, controls_height)
            self.right_label.setGeometry(right_x, right_y, right_width, right_height)
            self.metadata_frame.setGeometry(right_x, right_y, right_width, right_height)

    def set_both_images_visible(self, both_images_visible):
        self.both_images_visible = both_images_visible
        self.update_images()

    def left_image_path(self):
        return self.left_image_path_
    
    def right_image_path(self):
        return self.right_image_path_

    def clear_left_image(self):
        self.left_image_path_ = ''
        self.left_image = None
        self.update_images()

    def set_left_image(self, path):
        full_path = os.path.join(configuration.IMAGES_PATH, path)
        try:
            with Image.open(full_path) as image:
                self.left_metadata = ImageMetadata()
                self.left_metadata.path = path
                self.left_metadata.load_from_image_info(image.info)

                self.left_image_path_ = path
                self.left_image = pil_to_qimage(image)
        except (IOError, OSError):
            self.left_image_path_ = ''
            self.left_image = None
        self.update_images()

    def set_right_image(self, path):
        full_path = os.path.join(configuration.IMAGES_PATH, path)
        try:
            with Image.open(full_path) as image:
                self.metadata = ImageMetadata()
                self.metadata.path = path
                self.metadata.load_from_image_info(image.info)

                self.right_image_path_ = path
                self.right_image = pil_to_qimage(image)
        except (IOError, OSError):
            self.left_image_path_ = ''
            self.left_image = None

        self.metadata_frame.update(self.metadata)
        self.update_images()

    def set_preview_image(self, preview_image):
        if preview_image is not None:
            self.preview_image = pil_to_qimage(preview_image)
        else:
            self.preview_image = None
        self.update_images()

    def on_metadata_button_changed(self, state):
        self.metadata_frame.setVisible(state)

    def on_preview_button_changed(self, state):
        self.show_preview = state
        self.update_images()

class FloatSliderSpinBox(QWidget):
    def __init__(self, name, initial_value, checkable=False, parent=None):
        super().__init__(parent)

        if checkable:
            self.check_box = QCheckBox(name)
            self.check_box.setChecked(True)
            self.check_box.stateChanged.connect(self.on_check_box_changed)
        else:
            label = QLabel(name)
            label.setAlignment(Qt.AlignCenter)
        frame = QFrame()
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(1, 100)
        self.slider.setValue(initial_value * 100)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(10)
        self.slider.valueChanged.connect(self.on_slider_changed)
        self.spin_box = QDoubleSpinBox()
        self.spin_box.setAlignment(Qt.AlignCenter)
        self.spin_box.setFixedWidth(80)
        self.spin_box.setRange(0.01, 1.0)
        self.spin_box.setSingleStep(0.01)
        self.spin_box.setDecimals(2)
        self.spin_box.setValue(initial_value)
        self.spin_box.valueChanged.connect(self.on_spin_box_changed)

        hlayout = QHBoxLayout(frame)
        hlayout.setContentsMargins(0, 0, 0, 0)
        hlayout.addWidget(self.slider)
        hlayout.addWidget(self.spin_box)

        vlayout = QVBoxLayout(self)
        vlayout.setContentsMargins(0, 0, 0, 0) 
        vlayout.setSpacing(0)
        if checkable:
            check_box_layout = QHBoxLayout()
            check_box_layout.setAlignment(Qt.AlignCenter)
            check_box_layout.addWidget(self.check_box)
            vlayout.addLayout(check_box_layout)
        else:
            vlayout.addWidget(label)
        vlayout.addWidget(frame)

    def on_check_box_changed(self, state):
        self.slider.setEnabled(state)
        self.spin_box.setEnabled(state)

    def on_slider_changed(self, value):
        decimal_value = value / 100
        self.spin_box.setValue(decimal_value)

    def on_spin_box_changed(self, value):
        slider_value = round(value * 100)
        self.slider.setValue(slider_value)

class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint)
        self.setWindowTitle("About")

        layout = QVBoxLayout()

        app_info_label = QLabel(f"{configuration.APP_NAME}\nVersion {configuration.APP_VERSION}")
        app_info_label.setAlignment(Qt.AlignCenter)

        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.accept)

        layout.addWidget(app_info_label)
        layout.addWidget(ok_button)

        self.setLayout(layout)
        
class PreferencesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Preferences")
        self.settings = QSettings("settings.ini", QSettings.IniFormat)

        restartLabel = QLabel("Changes to application settings may require a restart.")

        self.reduce_memory = QCheckBox('Reduce Memory')
        self.reduce_memory.setChecked(self.settings.value('reduce_memory', type=bool))

        self.safety_checker = QCheckBox('Safety Checker')
        self.safety_checker.setChecked(self.settings.value('safety_checker', type=bool))

        models_group = QGroupBox()

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Display Name", "Repository ID"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)

        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_model)

        self.add_button = QPushButton("Add")
        self.add_button.clicked.connect(self.add_model)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.remove_button)
        button_layout.addWidget(self.add_button)

        models_group_layout = QVBoxLayout(models_group)
        models_group_layout.addWidget(self.table)
        models_group_layout.addLayout(button_layout)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(restartLabel)
        layout.addSpacing(8)
        layout.addWidget(self.reduce_memory)
        layout.addWidget(self.safety_checker)
        layout.addWidget(models_group)
        layout.addWidget(button_box)

        self.setLayout(layout)
        self.setMinimumWidth(600)

        self.load_models()

    def load_models(self):
        self.settings.beginGroup("Models")
        keys = self.settings.childKeys()
        self.table.setRowCount(len(keys))

        for i, key in enumerate(keys):
            display_name = key
            repo_id = self.settings.value(key)

            self.table.setItem(i, 0, QTableWidgetItem(display_name))
            self.table.setItem(i, 1, QTableWidgetItem(repo_id))

        self.settings.endGroup()

    def remove_model(self):
        current_row = self.table.currentRow()

        if current_row == -1:
            QMessageBox.warning(self, "Warning", "Please select a model to remove.")
            return

        self.table.removeRow(current_row)

    def add_model(self):
        add_dialog = QDialog(self)
        add_dialog.setWindowTitle("Add Model")

        vbox = QVBoxLayout()

        hbox1 = QHBoxLayout()
        hbox1.addWidget(QLabel("Display Name:"))
        display_name_edit = QLineEdit()
        hbox1.addWidget(display_name_edit)
        vbox.addLayout(hbox1)

        hbox2 = QHBoxLayout()
        hbox2.addWidget(QLabel("Repository ID:"))
        repo_id_edit = QLineEdit()
        hbox2.addWidget(repo_id_edit)
        vbox.addLayout(hbox2)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(add_dialog.accept)
        button_box.rejected.connect(add_dialog.reject)
        vbox.addWidget(button_box)

        add_dialog.setLayout(vbox)

        result = add_dialog.exec()
        if result == QDialog.Accepted:
            display_name = display_name_edit.text()
            repo_id = repo_id_edit.text()

            if not display_name or not repo_id:
                return

            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(display_name))
            self.table.setItem(row, 1, QTableWidgetItem(repo_id))

    def accept(self):
        self.settings.setValue('reduce_memory', self.reduce_memory.isChecked())
        self.settings.setValue('safety_checker', self.safety_checker.isChecked())

        self.settings.beginGroup("Models")
        self.settings.remove("")

        for row in range(self.table.rowCount()):
            display_name = self.table.item(row, 0).text()
            repo_id = self.table.item(row, 1).text()

            self.settings.setValue(display_name, repo_id)

        self.settings.endGroup()

        super().accept()

class CancelThreadException(Exception):
    pass

class GenerateThread(QThread):
    task_progress = Signal(int)
    image_preview = Signal(Image.Image)
    image_complete = Signal(str)
    task_complete = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self.cancel = False
        self.type = settings.value('type')
        self.collection = settings.value('collection')
        self.req = GenerateRequest()
        self.req.image_metadata.load_from_settings(settings)
        self.req.num_images_per_prompt = int(settings.value('num_images_per_prompt', 1))
        self.req.callback = self.generate_callback
    
    def run(self):
        try:
            self.run_()
        except CancelThreadException:
            pass
        except Exception as e:
            traceback.print_exc()
        
        self.task_complete.emit()
        gc.collect()
 
    def run_(self):
        global generate_preprocessor

        # load pipeline
        if self.type == 'txt2img':
            pipeline_type = Txt2ImgPipeline
        elif self.type == 'img2img':
            if self.req.image_metadata.control_net_model != '':
                pipeline_type = ControlNetPipeline
            else:
                pipeline_type = Img2ImgPipeline

        pipeline = pipeline_type(pipeline_cache, self.req.image_metadata)
        pipeline_cache.pipeline = pipeline
        pipe = pipeline.pipe

        # Source image
        if self.type == 'img2img':
            full_path = os.path.join(configuration.IMAGES_PATH, self.req.image_metadata.source_path)
            with Image.open(full_path) as image:
                image = image.convert('RGB')
                image = image.resize((self.req.image_metadata.width, self.req.image_metadata.height))
                self.req.source_image = image.copy()

            condition = configuration.conditions.get(self.req.image_metadata.condition, None)
            if isinstance(condition, ControlNetCondition) and self.req.image_metadata.control_net_preprocess:
                if not isinstance(generate_preprocessor, condition.preprocessor):
                    generate_preprocessor = condition.preprocessor()
                self.req.source_image = generate_preprocessor(self.req.source_image)
                if bool_setting('reduce_memory'):
                    generate_preprocessor = None

        # scheduler
        pipe.scheduler = configuration.schedulers[self.req.image_metadata.scheduler].from_config(pipe.scheduler.config)

        # generator
        self.req.generator = torch.Generator().manual_seed(self.req.image_metadata.seed)

        # prompt weighting
        compel_proc = Compel(tokenizer=pipe.tokenizer, text_encoder=pipe.text_encoder)
        self.req.prompt_embeds = compel_proc(self.req.image_metadata.prompt)
        self.req.negative_prompt_embeds = compel_proc(self.req.image_metadata.negative_prompt)

        # generate
        images = pipeline(self.req)

        step = self.compute_pipeline_steps()
        for image in images:
            # ESRGAN
            if self.req.image_metadata.upscale_enabled:
                esrgan = ESRGANProcessor()
                esrgan.upscale_factor = self.req.image_metadata.upscale_factor
                esrgan.denoising_strength = self.req.image_metadata.upscale_denoising_strength
                esrgan.blend_strength = self.req.image_metadata.upscale_blend_strength
                upscaled_image = esrgan(image)
            else:
                upscaled_image = image

            self.update_task_progress(step)
            step = step + 1

            # GFPGAN
            if self.req.image_metadata.face_enabled:
                gfpgan = GFPGANProcessor()
                gfpgan.upscale_factor = self.req.image_metadata.upscale_factor
                gfpgan.upscaled_image = upscaled_image
                gfpgan.blend_strength = self.req.image_metadata.face_blend_strength
                image = gfpgan(image)
            else:
                image = upscaled_image

            self.update_task_progress(step)
            step = step + 1

            # Output
            collection = self.collection
            png_info = PngImagePlugin.PngInfo()
            self.req.image_metadata.save_to_png_info(png_info)

            def io_operation():
                next_image_id = utils.next_image_id(os.path.join(configuration.IMAGES_PATH, collection))
                output_path = os.path.join(collection, '{:05d}.png'.format(next_image_id))
                full_path = os.path.join(configuration.IMAGES_PATH, output_path)

                image.save(full_path, pnginfo=png_info)
                return output_path

            output_path = utils.retry_on_failure(io_operation)

            self.update_task_progress(step)
            step = step + 1

            self.image_complete.emit(output_path)

    def generate_callback(self, step: int, timestep: int, latents: torch.FloatTensor):
        if self.cancel:
            raise CancelThreadException()
        self.update_task_progress(step)

        pil_image = latents_to_pil(latents)
        pil_image = pil_image.resize((pil_image.size[0] * 8, pil_image.size[1] * 8), Image.NEAREST)
        self.image_preview.emit(pil_image)

    def update_task_progress(self, step: int):
        steps = self.compute_total_steps()
        progress_amount = (step+1) * 100 / steps
        self.task_progress.emit(progress_amount)

    def compute_pipeline_steps(self):
        steps = self.req.image_metadata.num_inference_steps
        if self.type == 'img2img':
            condition = configuration.conditions.get(self.req.image_metadata.condition, None)
            if isinstance(condition, Img2ImgCondition):
                steps = int(self.req.image_metadata.num_inference_steps * self.req.image_metadata.img_strength)
        
        return steps

    def compute_total_steps(self):
        steps_per_image = 1
        if self.req.image_metadata.upscale_enabled:
            steps_per_image = steps_per_image + 1
        if self.req.image_metadata.face_enabled:
            steps_per_image = steps_per_image + 1

        return self.compute_pipeline_steps() + self.req.num_images_per_prompt * steps_per_image

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.generate_thread = None
        self.active_thread_count = 0

        self.setFocusPolicy(Qt.ClickFocus)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Menubar
        menu_bar = QMenuBar(self)

        action_about = actions.about.create(self)
        action_preferences = actions.preferences.create(self)

        action_generate_image = actions.generate_image.create(self)
        action_cancel_generation = actions.cancel_generation.create(self)
        action_send_to_img2img = actions.send_to_img2img.create(self)
        action_use_prompt = actions.use_prompt.create(self)
        action_use_seed = actions.use_seed.create(self)
        action_use_initial_image = actions.use_initial_image.create(self)
        action_use_all = actions.use_all.create(self)
        action_toggle_metadata = actions.toggle_metadata.create(self)
        action_toggle_preview = actions.toggle_preview.create(self)
        action_delete_image = actions.delete_image.create(self)
        action_reveal_in_finder = actions.reveal_in_finder.create(self)

        move_image_menu = QMenu('Move To', self)
        move_image_menu.setIcon(utils.empty_qicon())
        for collection in collections:
            item_action = QAction(collection, self)
            item_action.triggered.connect(lambda checked=False, x=collection: self.on_move_image(self.image_viewer.metadata, x))
            move_image_menu.addAction(item_action)

        app_menu = QMenu("Application", self)
        menu_bar.addMenu(app_menu)
        app_menu.addAction(action_about)
        app_menu.addSeparator()
        app_menu.addAction(action_preferences)

        image_menu = QMenu("Image", menu_bar)
        image_menu.addAction(action_generate_image)
        image_menu.addAction(action_cancel_generation)
        image_menu.addSeparator()
        image_menu.addAction(action_send_to_img2img)
        image_menu.addSeparator()
        image_menu.addAction(action_use_prompt)
        image_menu.addAction(action_use_seed)
        image_menu.addAction(action_use_initial_image)
        image_menu.addAction(action_use_all)
        image_menu.addSeparator()
        image_menu.addAction(action_toggle_metadata)
        image_menu.addAction(action_toggle_preview)
        image_menu.addSeparator()
        image_menu.addMenu(move_image_menu)
        image_menu.addAction(action_delete_image)
        image_menu.addSeparator()
        image_menu.addAction(action_reveal_in_finder)
        image_menu.addSeparator()

        action_about.triggered.connect(self.show_about_dialog)
        action_preferences.triggered.connect(self.show_preferences_dialog)

        action_generate_image.triggered.connect(self.on_generate_image)
        action_cancel_generation.triggered.connect(self.on_cancel_generation)
        action_send_to_img2img.triggered.connect(lambda: self.on_send_to_img2img(self.image_viewer.metadata))
        action_use_prompt.triggered.connect(lambda: self.on_use_prompt(self.image_viewer.metadata))
        action_use_seed.triggered.connect(lambda: self.on_use_seed(self.image_viewer.metadata))
        action_use_initial_image.triggered.connect(lambda: self.on_use_initial_image(self.image_viewer.metadata))
        action_use_all.triggered.connect(lambda: self.on_use_all(self.image_viewer.metadata))
        action_toggle_metadata.triggered.connect(lambda: self.image_viewer.toggle_metadata_button.toggle())
        action_toggle_preview.triggered.connect(lambda: self.image_viewer.toggle_preview_button.toggle())
        action_delete_image.triggered.connect(lambda: self.on_delete(self.image_viewer.metadata))
        action_reveal_in_finder.triggered.connect(lambda: self.on_reveal_in_finder(self.image_viewer.metadata))

        # Add the menu to the menu bar
        menu_bar.addMenu(image_menu)

        # Set the menu bar to the main window
        self.setMenuBar(menu_bar)

        # Modes
        mode_toolbar = QToolBar()
        mode_toolbar.setMovable(False)
        self.addToolBar(Qt.LeftToolBarArea, mode_toolbar)

        txt2img_button = actions.txt2img.tool_button()
        img2img_button = actions.img2img.tool_button()

        mode_toolbar.addWidget(txt2img_button)
        mode_toolbar.addWidget(img2img_button)

        self.button_group = QButtonGroup()
        self.button_group.addButton(txt2img_button, 0)
        self.button_group.addButton(img2img_button, 1)
        self.button_group.idToggled.connect(self.on_mode_changed)

        # Configuration controls
        config_frame = QFrame()
        config_frame.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        config_frame.setContentsMargins(0, 0, 0, 0)

        self.model_combo_box = QComboBox()
        settings.beginGroup('Models')
        for key in settings.childKeys():
            value = settings.value(key)
            index = self.model_combo_box.addItem(key, value)
        settings.endGroup()
        index = self.model_combo_box.findData(settings.value('model'))
        if index != -1:
            self.model_combo_box.setCurrentIndex(index)

        self.prompt_edit = PromptTextEdit(8, 'Prompt')
        self.prompt_edit.setPlainText(settings.value('prompt'))
        self.prompt_edit.return_pressed.connect(self.on_generate_image)
        self.negative_prompt_edit = PromptTextEdit(5, 'Negative Prompt')
        self.negative_prompt_edit.setPlainText(settings.value('negative_prompt'))
        self.negative_prompt_edit.return_pressed.connect(self.on_generate_image)

        self.generate_button = QPushButton('Generate')
        self.generate_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.generate_button.clicked.connect(self.on_generate_image)

        cancel_button = QPushButton()
        cancel_button.setIcon(QIcon(utils.resource_path('cancel_icon.png')))
        cancel_button.setToolTip('Cancel')
        cancel_button.clicked.connect(self.on_cancel_generation)

        controls_frame = QFrame()
        controls_frame.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        num_images_label = QLabel('Images')
        num_images_label.setAlignment(Qt.AlignCenter)
        self.num_images_spin_box = QSpinBox()
        self.num_images_spin_box.setAlignment(Qt.AlignCenter)
        self.num_images_spin_box.setFixedWidth(80)
        self.num_images_spin_box.setMinimum(1)
        self.num_images_spin_box.setValue(int(settings.value('num_images_per_prompt')))
        num_steps_label = QLabel('Steps')
        num_steps_label.setAlignment(Qt.AlignCenter)
        self.num_steps_spin_box = QSpinBox()
        self.num_steps_spin_box.setAlignment(Qt.AlignCenter)
        self.num_steps_spin_box.setFixedWidth(80)
        self.num_steps_spin_box.setMinimum(1)
        self.num_steps_spin_box.setValue(int(settings.value('num_inference_steps')))
        guidance_scale_label = QLabel('CFG Scale')
        guidance_scale_label.setAlignment(Qt.AlignCenter)
        self.guidance_scale_spin_box = QDoubleSpinBox()
        self.guidance_scale_spin_box.setAlignment(Qt.AlignCenter)
        self.guidance_scale_spin_box.setFixedWidth(80)
        self.guidance_scale_spin_box.setSingleStep(0.5)
        self.guidance_scale_spin_box.setMinimum(1.0)
        self.guidance_scale_spin_box.setValue(float(settings.value('guidance_scale')))
        width_label = QLabel('Width')
        width_label.setAlignment(Qt.AlignCenter)
        self.width_spin_box = QSpinBox()
        self.width_spin_box.setAlignment(Qt.AlignCenter)
        self.width_spin_box.setFixedWidth(80)
        self.width_spin_box.setSingleStep(64)
        self.width_spin_box.setMinimum(64)
        self.width_spin_box.setMaximum(1024)
        self.width_spin_box.setValue(int(settings.value('width')))
        height_label = QLabel('Height')
        height_label.setAlignment(Qt.AlignCenter)
        self.height_spin_box = QSpinBox()
        self.height_spin_box.setAlignment(Qt.AlignCenter)
        self.height_spin_box.setFixedWidth(80)
        self.height_spin_box.setSingleStep(64)
        self.height_spin_box.setMinimum(64)
        self.height_spin_box.setMaximum(1024)
        self.height_spin_box.setValue(int(settings.value('height')))
        scheduler_label = QLabel('Scheduler')
        scheduler_label.setAlignment(Qt.AlignCenter)
        self.scheduler_combo_box = QComboBox()
        self.scheduler_combo_box.addItems(configuration.schedulers.keys())
        self.scheduler_combo_box.setFixedWidth(120)
        self.scheduler_combo_box.setCurrentText(settings.value('scheduler'))

        self.manual_seed_check_box = QCheckBox('Manual Seed')

        self.seed_frame = QFrame()
        self.seed_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.seed_lineedit = QLineEdit()
        self.seed_lineedit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.seed_lineedit.setText(str(settings.value('seed')))
        seed_random_button = QPushButton('New')
        seed_random_button.clicked.connect(self.on_seed_random_clicked)

        manual_seed = bool_setting('manual_seed')
        self.seed_frame.setEnabled(manual_seed)
        self.manual_seed_check_box.setChecked(manual_seed)
        self.manual_seed_check_box.stateChanged.connect(self.on_manual_seed_check_box_changed)

        self.condition_frame = QFrame()
        conditions_label = QLabel('Condition: ')
        self.condition_combo_box = QComboBox()
        self.condition_combo_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.condition_combo_box.addItems(configuration.conditions.keys())
        self.condition_combo_box.setCurrentText(settings.value('condition'))
        self.condition_combo_box.currentIndexChanged.connect(self.on_condition_combobox_value_changed)

        self.control_net_frame = QFrame()
        self.control_net_preprocess_check_box = QCheckBox('Preprocess')
        self.control_net_preprocess_check_box.setChecked(bool_setting('control_net_preprocess'))
        self.control_net_preview_preprocessor_button = QPushButton('Preview')
        self.control_net_preview_preprocessor_button.clicked.connect(self.on_control_net_preview_preprocessor_button_clicked)

        control_net_model_label = QLabel('Model')
        control_net_model_label.setAlignment(Qt.AlignCenter)
        self.control_net_model_combo_box = QComboBox()

        self.control_net_scale = FloatSliderSpinBox('ControlNet Scale', float(settings.value('control_net_scale')))

        control_net_grid = QGridLayout()
        control_net_grid.setContentsMargins(0, 0, 0, 0)
        control_net_grid.setVerticalSpacing(2)
        control_net_grid.addWidget(self.control_net_preprocess_check_box, 0, 0)
        control_net_grid.setAlignment(self.control_net_preprocess_check_box, Qt.AlignCenter)
        control_net_grid.addWidget(self.control_net_preview_preprocessor_button, 1, 0)
        control_net_grid.addWidget(control_net_model_label, 0, 1)
        control_net_grid.addWidget(self.control_net_model_combo_box, 1, 1)

        self.img_strength = FloatSliderSpinBox('Image Strength', float(settings.value('img_strength')))

        upscale_enabled = settings.value('upscale_enabled', type=bool)
        self.upscale_enabled_check_box = QCheckBox('Upscaling')
        self.upscale_enabled_check_box.setChecked(upscale_enabled)
        self.upscale_enabled_check_box.stateChanged.connect(self.on_upscale_enabled_check_box_changed)
        self.upscale_frame = QFrame()
        self.upscale_frame.setEnabled(upscale_enabled)
        upscale_factor_label = QLabel('Factor: ')
        self.upscale_factor_combo_box = QComboBox()
        self.upscale_factor_combo_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.upscale_factor_combo_box.addItem('2x', 2)
        self.upscale_factor_combo_box.addItem('4x', 4)
        index = self.upscale_factor_combo_box.findData(settings.value('upscale_factor', type=int))
        if index != -1:
            self.upscale_factor_combo_box.setCurrentIndex(index)        
        self.upscale_denoising_strength = FloatSliderSpinBox('Denoising Strength', float(settings.value('upscale_denoising_strength')))
        self.upscale_blend_strength = FloatSliderSpinBox('Upscale Strength', float(settings.value('upscale_denoising_strength')))
        self.face_strength = FloatSliderSpinBox('Face Restoration', float(settings.value('face_blend_strength')), checkable=True)
        self.face_strength.check_box.setChecked(bool_setting('face_enabled'))

        generate_hlayout = QHBoxLayout()
        generate_hlayout.setContentsMargins(0, 0, 0, 0)
        generate_hlayout.addWidget(self.generate_button)
        generate_hlayout.addWidget(cancel_button)

        controls_grid = QGridLayout(controls_frame)
        controls_grid.setContentsMargins(0, 0, 0, 0)
        controls_grid.setVerticalSpacing(2)
        controls_grid.setRowMinimumHeight(2, 10)
        controls_grid.addWidget(num_images_label, 0, 0)
        controls_grid.addWidget(self.num_images_spin_box, 1, 0)
        controls_grid.addWidget(num_steps_label, 0, 1)
        controls_grid.addWidget(self.num_steps_spin_box, 1, 1)
        controls_grid.addWidget(guidance_scale_label, 0, 2)
        controls_grid.addWidget(self.guidance_scale_spin_box, 1, 2)
        controls_grid.setAlignment(self.guidance_scale_spin_box, Qt.AlignCenter)
        controls_grid.addWidget(width_label, 3, 0)
        controls_grid.addWidget(self.width_spin_box, 4, 0)
        controls_grid.addWidget(height_label, 3, 1)
        controls_grid.addWidget(self.height_spin_box, 4, 1)
        controls_grid.addWidget(scheduler_label, 3, 2)
        controls_grid.addWidget(self.scheduler_combo_box, 4, 2)

        seed_hlayout = QHBoxLayout(self.seed_frame)
        seed_hlayout.setContentsMargins(0, 0, 0, 0)
        seed_hlayout.addWidget(self.seed_lineedit)
        seed_hlayout.addWidget(seed_random_button)

        seed_vlayout = QVBoxLayout()
        seed_vlayout.setContentsMargins(0, 0, 0, 0) 
        seed_vlayout.setSpacing(0)
        seed_check_box_layout = QHBoxLayout()
        seed_check_box_layout.setAlignment(Qt.AlignCenter)
        seed_check_box_layout.addWidget(self.manual_seed_check_box)
        seed_vlayout.addLayout(seed_check_box_layout)
        seed_vlayout.addWidget(self.seed_frame)

        condition_frame_layout = QHBoxLayout(self.condition_frame)
        condition_frame_layout.setContentsMargins(0, 0, 0, 0)
        condition_frame_layout.addWidget(conditions_label)
        condition_frame_layout.addWidget(self.condition_combo_box)

        control_net_layout = QVBoxLayout(self.control_net_frame)
        control_net_layout.setContentsMargins(0, 0, 0, 0)
        control_net_layout.setSpacing(0)
        control_net_layout.addLayout(control_net_grid)
        control_net_layout.addWidget(self.control_net_scale)
        control_net_layout.addWidget(create_frame_separator())

        condition_layout = QVBoxLayout()
        condition_layout.setContentsMargins(0, 0, 0, 0) 
        condition_layout.setSpacing(0)
        condition_layout.addWidget(self.condition_frame)
        condition_layout.addWidget(self.control_net_frame)
        condition_layout.addWidget(self.img_strength)

        upscale_factor_layout = QHBoxLayout()
        upscale_factor_layout.setContentsMargins(0, 0, 0, 0) 
        upscale_factor_layout.setSpacing(0)
        upscale_factor_layout.addWidget(upscale_factor_label)
        upscale_factor_layout.addWidget(self.upscale_factor_combo_box)

        upscale_frame_layout = QVBoxLayout(self.upscale_frame)
        upscale_frame_layout.setContentsMargins(0, 0, 0, 0)
        upscale_frame_layout.setSpacing(0)
        upscale_frame_layout.addLayout(upscale_factor_layout)
        upscale_frame_layout.addWidget(self.upscale_denoising_strength)
        upscale_frame_layout.addWidget(self.upscale_blend_strength)

        upscale_enabled_check_box_layout = QVBoxLayout()
        upscale_enabled_check_box_layout.setContentsMargins(0, 0, 0, 0) 
        upscale_enabled_check_box_layout.setSpacing(0)
        upscale_enabled_check_box_layout.addWidget(self.upscale_enabled_check_box)
        upscale_enabled_check_box_layout.setAlignment(self.upscale_enabled_check_box, Qt.AlignCenter)

        upscale_layout = QVBoxLayout()
        upscale_layout.setContentsMargins(0, 0, 0, 0) 
        upscale_layout.setSpacing(0)
        upscale_layout.addLayout(upscale_enabled_check_box_layout)
        upscale_layout.addWidget(self.upscale_frame)

        config_layout = QVBoxLayout(config_frame)
        config_layout.setContentsMargins(0, 0, 0, 0) 
        config_layout.addWidget(self.model_combo_box)
        config_layout.addWidget(self.prompt_edit)
        config_layout.addWidget(self.negative_prompt_edit)
        config_layout.addLayout(generate_hlayout)
        config_layout.addWidget(create_frame_separator())
        config_layout.addWidget(controls_frame)
        config_layout.addWidget(create_frame_separator())
        config_layout.addLayout(seed_vlayout)
        config_layout.addWidget(create_frame_separator())
        config_layout.addLayout(condition_layout)
        config_layout.addLayout(upscale_layout)
        config_layout.addWidget(create_frame_separator())
        config_layout.addWidget(self.face_strength)
        config_layout.addStretch()

        # Image viewer
        self.image_viewer = ImageViewer()
        self.image_viewer.locate_source_button.pressed.connect(lambda: self.thumbnail_viewer.select_image(self.image_viewer.left_image_path()))
        self.image_viewer.send_to_img2img_button.pressed.connect(lambda: self.on_send_to_img2img(self.image_viewer.metadata))
        self.image_viewer.use_prompt_button.pressed.connect(lambda: self.on_use_prompt(self.image_viewer.metadata))
        self.image_viewer.use_seed_button.pressed.connect(lambda: self.on_use_seed(self.image_viewer.metadata))
        self.image_viewer.use_initial_image_button.pressed.connect(lambda: self.on_use_initial_image(self.image_viewer.metadata))
        self.image_viewer.use_all_button.pressed.connect(lambda: self.on_use_all(self.image_viewer.metadata))
        self.image_viewer.delete_button.pressed.connect(lambda: self.on_delete(self.image_viewer.metadata))

        # Thumbnail viewer
        self.thumbnail_viewer = ThumbnailViewer()
        self.thumbnail_viewer.file_dropped.connect(self.on_thumbnail_file_dropped)
        self.thumbnail_viewer.list_widget.itemSelectionChanged.connect(self.on_thumbnail_selection_change)
        self.thumbnail_viewer.action_send_to_img2img.triggered.connect(lambda: self.on_send_to_img2img(self.thumbnail_viewer.get_current_metadata()))
        self.thumbnail_viewer.action_use_prompt.triggered.connect(lambda: self.on_use_prompt(self.thumbnail_viewer.get_current_metadata()))
        self.thumbnail_viewer.action_use_seed.triggered.connect(lambda: self.on_use_seed(self.thumbnail_viewer.get_current_metadata()))
        self.thumbnail_viewer.action_use_initial_image.triggered.connect(lambda: self.on_use_initial_image(self.thumbnail_viewer.get_current_metadata()))
        self.thumbnail_viewer.action_use_all.triggered.connect(lambda: self.on_use_all(self.thumbnail_viewer.get_current_metadata()))
        self.thumbnail_viewer.action_delete.triggered.connect(lambda: self.on_delete(self.thumbnail_viewer.get_current_metadata()))
        self.thumbnail_viewer.action_reveal_in_finder.triggered.connect(lambda: self.on_reveal_in_finder(self.thumbnail_viewer.get_current_metadata()))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.image_viewer)
        splitter.addWidget(self.thumbnail_viewer)
        splitter.setStretchFactor(0, 1)  # left widget
        splitter.setStretchFactor(1, 0)  # right widget

        palette = QApplication.instance().palette()
        background_color = palette.color(QPalette.Window)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setMinimum(0)

        hlayout = QHBoxLayout()
        hlayout.setContentsMargins(8, 2, 8, 8)
        hlayout.setSpacing(8)
        hlayout.addWidget(config_frame)
        hlayout.addWidget(splitter)

        vlayout = QVBoxLayout(central_widget)
        vlayout.setContentsMargins(0, 0, 0, 0)
        vlayout.setSpacing(0)
        vlayout.addWidget(self.progress_bar)
        vlayout.addLayout(hlayout)

        self.setWindowTitle(configuration.APP_NAME)
        self.setGeometry(100, 100, 1200, 600)

        # Apply settings that impact other controls
        if settings.value('source_path') != '':
            self.image_viewer.set_left_image(settings.value('source_path'))
        self.set_type(settings.value('type'))
        self.on_thumbnail_selection_change()
        self.on_condition_combobox_value_changed(self.condition_combo_box.currentIndex())

    def show_about_dialog(self):
        about_dialog = AboutDialog()
        about_dialog.exec()

    def show_preferences_dialog(self):
        dialog = PreferencesDialog(self)
        dialog.exec()
        
    def set_type(self, type):
        self.type = type
        if self.type == 'txt2img':
            self.button_group.button(0).setChecked(True)
        elif self.type == 'img2img':
            self.button_group.button(1).setChecked(True)

    def on_mode_changed(self, button_id, checked):
        if not checked:
            return
        if button_id == 0:
            self.type = 'txt2img'
        elif button_id == 1:
            self.type = 'img2img'
        
        self.update_control_visibility()

    def on_cancel_generation(self):
        if self.generate_thread:
            self.generate_thread.cancel = True

    def on_condition_combobox_value_changed(self, index):
        condition_name = self.condition_combo_box.itemText(index)
        condition = configuration.conditions.get(condition_name, None)
        if isinstance(condition, Img2ImgCondition):
            pass
        elif isinstance(condition, ControlNetCondition):
            self.control_net_model_combo_box.clear()
            for key, value in condition.models.items():
                self.control_net_model_combo_box.addItem(key, value)
            self.control_net_model_combo_box.setCurrentText(settings.value('control_net_model'))

        self.update_control_visibility()

    def on_control_net_preview_preprocessor_button_clicked(self):
        source_path = self.image_viewer.left_image_path()
        width = self.width_spin_box.value()
        height = self.height_spin_box.value()

        full_path = os.path.join(configuration.IMAGES_PATH, source_path)
        with Image.open(full_path) as image:
            image = image.convert('RGB')
            image = image.resize((width, height))
            source_image = image.copy()

        condition_name = self.condition_combo_box.currentText()
        condition = configuration.conditions.get(condition_name, None)
        if isinstance(condition, ControlNetCondition):
            global preview_preprocessor
            if not isinstance(preview_preprocessor, condition.preprocessor):
                preview_preprocessor = condition.preprocessor()
            source_image = preview_preprocessor(source_image)
            if bool_setting('reduce_memory'):
                preview_preprocessor = None
            output_path = 'preprocessed.png'
            full_path = os.path.join(configuration.IMAGES_PATH, output_path)
            source_image.save(full_path)
            self.image_viewer.set_right_image(output_path)

    def update_control_visibility(self):
        if self.type == 'txt2img':
            self.condition_frame.setVisible(False)
            self.img_strength.setVisible(False)
            self.control_net_frame.setVisible(False)
            self.image_viewer.set_both_images_visible(False)
        elif self.type == 'img2img':
            condition_name = self.condition_combo_box.currentText()
            condition = configuration.conditions.get(condition_name, None)
            self.condition_frame.setVisible(True)
            if isinstance(condition, Img2ImgCondition):
                self.img_strength.setVisible(True)
                self.control_net_frame.setVisible(False)
            elif isinstance(condition, ControlNetCondition):
                self.img_strength.setVisible(False)
                self.control_net_frame.setVisible(True)
            self.image_viewer.set_both_images_visible(True)

    def on_generate_image(self):
        if not self.manual_seed_check_box.isChecked():
            self.randomize_seed()

        condition_name = self.condition_combo_box.currentText()
        condition = configuration.conditions.get(condition_name, None)

        settings.setValue('collection', self.thumbnail_viewer.collection())
        settings.setValue('type', self.type)
        settings.setValue('model', self.model_combo_box.currentData())
        settings.setValue('scheduler', self.scheduler_combo_box.currentText())
        settings.setValue('prompt', self.prompt_edit.toPlainText())
        settings.setValue('negative_prompt', self.negative_prompt_edit.toPlainText())
        settings.setValue('manual_seed', self.manual_seed_check_box.isChecked())
        settings.setValue('seed', self.seed_lineedit.text())
        settings.setValue('num_images_per_prompt', self.num_images_spin_box.value())
        settings.setValue('num_inference_steps', self.num_steps_spin_box.value())
        settings.setValue('guidance_scale', self.guidance_scale_spin_box.value())
        settings.setValue('width', self.width_spin_box.value())
        settings.setValue('height', self.height_spin_box.value())
        settings.setValue('condition', condition_name)
        if isinstance(condition, ControlNetCondition):
            settings.setValue('control_net_preprocess', self.control_net_preprocess_check_box.isChecked())
            settings.setValue('control_net_model', self.control_net_model_combo_box.currentData())
            settings.setValue('control_net_scale', self.control_net_scale.spin_box.value())
        settings.setValue('source_path', self.image_viewer.left_image_path())
        settings.setValue('img_strength', self.img_strength.spin_box.value())
        settings.setValue('upscale_enabled', self.upscale_enabled_check_box.isChecked())
        settings.setValue('upscale_factor', self.upscale_factor_combo_box.currentData())
        settings.setValue('upscale_denoising_strength', self.upscale_denoising_strength.spin_box.value())
        settings.setValue('upscale_blend_strength', self.upscale_blend_strength.spin_box.value())
        settings.setValue('face_enabled', self.face_strength.check_box.isChecked())
        settings.setValue('face_blend_strength', self.face_strength.spin_box.value())

        self.update_progress(0, 0)
        self.generate_button.setEnabled(False)
        self.generate_thread = GenerateThread(self)
        self.generate_thread.task_progress.connect(self.update_progress)
        self.generate_thread.image_preview.connect(self.image_preview)
        self.generate_thread.image_complete.connect(self.image_complete)
        self.generate_thread.task_complete.connect(self.generate_complete)
        self.generate_thread.start()

    def update_progress(self, progress_amount, maximum_amount=100):
        self.progress_bar.setMaximum(maximum_amount)
        if maximum_amount == 0:
            self.progress_bar.setStyleSheet('QProgressBar:chunk { background-color: grey; }')
        else:
            self.progress_bar.setStyleSheet('QProgressBar:chunk { background-color: blue; }')
        if progress_amount is not None:
            self.progress_bar.setValue(progress_amount)
        else:
            self.progress_bar.setValue(0)

        if sys.platform == 'darwin':
            sharedApplication = NSApplication.sharedApplication()
            dockTile = sharedApplication.dockTile()
            if maximum_amount == 0:
                dockTile.setBadgeLabel_('...')
            elif progress_amount is not None:
                dockTile.setBadgeLabel_('{:d}%'.format(progress_amount))
            else:
                dockTile.setBadgeLabel_(None)

    def image_preview(self, preview_image):
        self.image_viewer.set_preview_image(preview_image)

    def image_complete(self, output_path):
        self.on_add_file(output_path)

    def generate_complete(self):
        self.generate_button.setEnabled(True)
        self.update_progress(None)
        self.image_viewer.set_preview_image(None)
        self.generate_thread = None

    def randomize_seed(self):
        seed = random.randint(0, 0x7fff_ffff_ffff_ffff)
        self.seed_lineedit.setText(str(seed))

    def on_manual_seed_check_box_changed(self, state):
        self.seed_frame.setEnabled(state)

    def on_seed_random_clicked(self):
        self.randomize_seed()

    def on_upscale_enabled_check_box_changed(self, state):
        self.upscale_frame.setEnabled(state)

    def on_thumbnail_file_dropped(self, source_path: str):
        collection = self.thumbnail_viewer.collection()

        def io_operation():
            next_image_id = utils.next_image_id(os.path.join(configuration.IMAGES_PATH, collection))
            output_path = os.path.join(collection, '{:05d}.png'.format(next_image_id))
            full_path = os.path.join(configuration.IMAGES_PATH, output_path)

            with Image.open(source_path) as image:
                metadata = ImageMetadata()
                metadata.path = output_path
                metadata.load_from_image_info(image.info)
                png_info = PngImagePlugin.PngInfo()
                metadata.save_to_png_info(png_info)

                image.save(full_path, pnginfo = png_info)
            return output_path

        output_path = utils.retry_on_failure(io_operation)

        self.on_add_file(output_path)

    def on_thumbnail_selection_change(self):
        selected_items = self.thumbnail_viewer.list_widget.selectedItems()
        for item in selected_items:
            rel_path = item.data(Qt.UserRole)
            self.image_viewer.set_right_image(rel_path)

    def on_send_to_img2img(self, image_metadata):
        if image_metadata is not None:
            self.image_viewer.set_left_image(image_metadata.path)
            self.set_type('img2img')
    
    def on_use_prompt(self, image_metadata):
        if image_metadata is not None:
            self.prompt_edit.setPlainText(image_metadata.prompt)
            self.negative_prompt_edit.setPlainText(image_metadata.negative_prompt)

    def on_use_seed(self, image_metadata):
        if image_metadata is not None:
            self.manual_seed_check_box.setChecked(True)
            self.seed_lineedit.setText(str(image_metadata.seed))

    def on_use_initial_image(self, image_metadata):
        if image_metadata is not None:
            self.image_viewer.set_left_image(image_metadata.source_path)
            self.img_strength.spin_box.setValue(image_metadata.img_strength)
            self.set_type('img2img')
 
    def on_use_all(self, image_metadata):
        if image_metadata is not None:
            self.prompt_edit.setPlainText(image_metadata.prompt)
            self.negative_prompt_edit.setPlainText(image_metadata.negative_prompt)
            self.manual_seed_check_box.setChecked(True)
            self.seed_lineedit.setText(str(image_metadata.seed))
            self.num_steps_spin_box.setValue(image_metadata.num_inference_steps)
            self.guidance_scale_spin_box.setValue(image_metadata.guidance_scale)
            self.width_spin_box.setValue(image_metadata.width)
            self.height_spin_box.setValue(image_metadata.height)
            self.scheduler_combo_box.setCurrentText(image_metadata.scheduler)
            if image_metadata.type == 'img2img':
                self.image_viewer.set_left_image(image_metadata.source_path)
                self.condition_combo_box.setCurrentText(image_metadata.condition)
                condition = configuration.conditions.get(image_metadata.condition, None)
                if isinstance(condition, Img2ImgCondition):
                    self.img_strength.spin_box.setValue(image_metadata.img_strength)
                if isinstance(condition, ControlNetCondition):
                    self.control_net_preprocess_check_box.setChecked(image_metadata.control_net_preprocess)
                    self.control_net_model_combo_box.setCurrentText(image_metadata.control_net_model)

            if image_metadata.upscale_enabled:
                self.upscale_enabled_check_box.setChecked(True)
                index = self.upscale_factor_combo_box.findData(image_metadata.upscale_factor)
                if index != -1:
                    self.upscale_factor_combo_box.setCurrentIndex(index)
                self.upscale_denoising_strength.spin_box.setValue(image_metadata.upscale_denoising_strength)
                self.upscale_blend_strength.spin_box.setValue(image_metadata.upscale_blend_strength)
            else:
                self.upscale_enabled_check_box.setChecked(False)

            if image_metadata.face_enabled:
                self.face_strength.check_box.setChecked(True)
                self.face_strength.spin_box.setValue(image_metadata.face_blend_strength)
            else:
                self.face_strength.check_box.setChecked(False)

            self.set_type(image_metadata.type)

    def on_move_image(self, image_metadata: ImageMetadata, collection: str):
        current_collection = self.thumbnail_viewer.collection()
        if collection == current_collection:
            return

        if image_metadata is not None:
            source_path = image_metadata.path

            def io_operation():
                next_image_id = utils.next_image_id(os.path.join(configuration.IMAGES_PATH, collection))
                output_path = os.path.join(collection, '{:05d}.png'.format(next_image_id))

                full_source_path = os.path.join(configuration.IMAGES_PATH, source_path)
                full_output_path = os.path.join(configuration.IMAGES_PATH, output_path)

                shutil.move(full_source_path, full_output_path)

            output_path = utils.retry_on_failure(io_operation)

            self.on_remove_file(image_metadata.path)
            self.on_add_file(output_path)

    def on_delete(self, image_metadata):
        if image_metadata is not None:
            message_box = QMessageBox()
            message_box.setIcon(QMessageBox.Warning)
            message_box.setWindowTitle('Confirm Delete')
            message_box.setText('Are you sure you want to delete this image?')
            message_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            message_box.setDefaultButton(QMessageBox.No)

            result = message_box.exec()
            if result == QMessageBox.Yes:
                full_path = os.path.join(configuration.IMAGES_PATH, image_metadata.path)
                utils.recycle_file(full_path)
                self.on_remove_file(image_metadata.path)

    def on_add_file(self, path):
        collection = os.path.dirname(path)
        current_collection = self.thumbnail_viewer.collection()
        if collection != current_collection:
            return

        self.thumbnail_viewer.add_image(path)
        self.thumbnail_viewer.list_widget.setCurrentRow(0)
        self.image_viewer.set_right_image(path)

    def on_remove_file(self, path):
        full_thumbnail_path = os.path.join(configuration.THUMBNAILS_PATH, path)
        os.remove(full_thumbnail_path)

        self.thumbnail_viewer.remove_image(path)
        if self.image_viewer.left_image_path() == path:
            self.image_viewer.clear_left_image()

    def on_reveal_in_finder(self, image_metadata):
        if image_metadata is not None:
            full_path = os.path.abspath(os.path.join(configuration.IMAGES_PATH, image_metadata.path))
            utils.reveal_in_finder(full_path)

    def hide_if_thread_running(self):
        if self.generate_thread:
            self.active_thread_count = self.active_thread_count + 1
            self.generate_thread.cancel = True
            self.generate_thread.finished.connect(self.thread_finished)

        if self.active_thread_count > 0:
            self.hide()
            return True
        else:
            return False

    def thread_finished(self):
        self.active_thread_count = self.active_thread_count - 1
        if self.active_thread_count == 0:
            QApplication.instance().quit()

    def closeEvent(self, event):
        if self.hide_if_thread_running():
            event.ignore()
        else:
            event.accept()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Left:
            self.thumbnail_viewer.previous_image()
        elif key == Qt.Key_Right:
            self.thumbnail_viewer.next_image()
        else:
            super().keyPressEvent(event)

class Application(QApplication):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Settings
        global settings
        settings = QSettings('settings.ini', QSettings.IniFormat)
        set_default_setting('safety_checker', True)
        set_default_setting('collection', 'outputs')
        set_default_setting('type', 'txt2img')
        set_default_setting('scheduler', 'k_euler_a')
        set_default_setting('model', 'runwayml/stable-diffusion-v1-5')
        set_default_setting('prompt', '')
        set_default_setting('negative_prompt', '')
        set_default_setting('manual_seed', False)
        set_default_setting('seed', 1)
        set_default_setting('num_images_per_prompt', 1)
        set_default_setting('num_inference_steps', 30)
        set_default_setting('guidance_scale', 7.5)
        set_default_setting('width', 512)
        set_default_setting('height', 512)
        set_default_setting('condition', 'Image')
        set_default_setting('control_net_preprocess', True)
        set_default_setting('control_net_model', '')
        set_default_setting('control_net_scale', 1.0)
        set_default_setting('source_path', '')
        set_default_setting('img_strength', 0.5)
        set_default_setting('upscale_enabled', False)
        set_default_setting('upscale_factor', 2)
        set_default_setting('upscale_denoising_strength', 0.75)
        set_default_setting('upscale_blend_strength', 0.75)
        set_default_setting('face_enabled', False)
        set_default_setting('face_blend_strength', 0.75)
        set_default_setting('reduce_memory', True)
        settings.beginGroup('Models')
        set_default_setting('Stable Diffusion v1-5', 'runwayml/stable-diffusion-v1-5')
        settings.endGroup()

        self.setWindowIcon(QIcon(utils.resource_path('app_icon.png')))
        self.setApplicationName(configuration.APP_NAME)
        self.setStyleSheet('''
        QToolButton {
            background-color: rgba(50, 50, 50, 255);
        }
        QToolButton:hover {
            background-color: darkgrey;
        }
        QToolButton:checked {
            background-color: darkblue;
        }
        QToolButton:pressed {
            background-color: darkblue;
        }
        ''')
        self.main_window = MainWindow()
        self.main_window.show()

    def event(self, event):
        if event.type() == QEvent.Quit:
            if self.main_window.hide_if_thread_running():
                return False
        return super().event(event)

def main():
    os.makedirs(configuration.IMAGES_PATH, exist_ok=True)
    os.makedirs(configuration.THUMBNAILS_PATH, exist_ok=True)
    os.makedirs(configuration.MODELS_PATH, exist_ok=True)    

    if sys.platform == 'darwin':
        bundle = NSBundle.mainBundle()
        info_dict = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        info_dict['CFBundleName'] = configuration.APP_NAME

    global collections
    collections = sorted([entry for entry in os.listdir(configuration.IMAGES_PATH) if os.path.isdir(os.path.join(configuration.IMAGES_PATH, entry))])
    if not collections:
        os.makedirs(os.path.join(configuration.IMAGES_PATH, 'outputs'))
        collections = ['outputs']

    app = Application(sys.argv)
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
