from dataclasses import dataclass
from typing import Dict
from typing import Iterable, Optional
import types
import time
import numpy as np
import torch
import torch.nn.functional as F
from torch import Tensor
from torch import nn
from torch.cuda.amp import autocast
from funasr.metrics.compute_acc import compute_accuracy
from funasr.losses.label_smoothing_loss import LabelSmoothingLoss
from funasr.train_utils.device_funcs import force_gatherable
from . import whisper_lib as whisper
from funasr.utils.load_utils import load_audio_text_image_video, extract_fbank

from funasr.register import tables




@tables.register("model_classes", "SenseVoice")
class SenseVoice(nn.Module):
    def __init__(self, *args, **kwargs):
        super().__init__()
        
        dims = kwargs.get("dims", {})
        dims = whisper.model.ModelDimensions(**dims)
        model = whisper.model.Whisper(dims=dims)
        
        # encoder
        model.encoder.downsample_rate = kwargs.get("downsample_rate", 4)
        model.encoder.use_padmask = kwargs.get("use_padmask", True)
        from .encoder import sense_voice_encode_forward
        model.encoder.forward = types.MethodType(sense_voice_encode_forward, model.encoder)
        
        # decoder
        model.decoder.use_padmask = kwargs.get("use_padmask", True)
        from .decoder import sense_voice_decode_forward
        model.decoder.forward = types.MethodType(sense_voice_decode_forward, model.decoder)
        
        self.model = model
        
        self.encoder_output_size = self.model.dims.n_audio_state
        
        self.activation_checkpoint = kwargs.get("activation_checkpoint", False)
        self.ignore_id = kwargs.get("ignore_id", -1)
        self.vocab_size = kwargs.get("vocab_size", -1)
        self.length_normalized_loss = kwargs.get("length_normalized_loss", True)
        self.criterion_att = LabelSmoothingLoss(
            size=self.vocab_size,
            padding_idx=self.ignore_id,
            smoothing=kwargs.get("lsm_weight", 0.0),
            normalize_length=self.length_normalized_loss,
        )
        
        specaug = kwargs.get("specaug", None)
        if specaug is not None:
            specaug_class = tables.specaug_classes.get(specaug)
            specaug = specaug_class(**kwargs.get("specaug_conf", {}))
        self.specaug = specaug

 
    def forward(
        self,
        speech: torch.Tensor,
        speech_lengths: torch.Tensor,
        text: torch.Tensor,
        text_lengths: torch.Tensor,
        **kwargs,
    ):
        target_mask = kwargs.get("target_mask", None)
    
        # import pdb;
        # pdb.set_trace()
        if len(text_lengths.size()) > 1:
            text_lengths = text_lengths[:, 0]
        if len(speech_lengths.size()) > 1:
            speech_lengths = speech_lengths[:, 0]
    
        batch_size = speech.shape[0]

        if self.activation_checkpoint:
            from torch.utils.checkpoint import checkpoint
            encoder_out, encoder_out_lens = checkpoint(self.encode, speech, speech_lengths, use_reentrant=False)
        else:
            encoder_out, encoder_out_lens = self.encode(speech, speech_lengths)

        loss_att, acc_att, cer_att, wer_att = self._calc_att_loss(
            encoder_out, encoder_out_lens, text, text_lengths, target_mask=target_mask
        )
        loss = loss_att
        stats = {}
        stats["acc"] = acc_att
        stats["loss"] = torch.clone(loss.detach())
        stats["batch_size"] = batch_size
        
        # force_gatherable: to-device and to-tensor if scalar for DataParallel
        if self.length_normalized_loss:
            batch_size = int((text_lengths + 1).sum())
        loss, stats, weight = force_gatherable((loss, stats, batch_size), loss.device)
        return loss, stats, weight

    def encode(
        self, speech: torch.Tensor, speech_lengths: torch.Tensor, **kwargs,
    ) :
        """Encoder. Note that this method is used by asr_inference.py
        Args:
                speech: (Batch, Length, ...)
                speech_lengths: (Batch, )
                ind: int
        """
        with autocast(False):

            # Data augmentation
            if self.specaug is not None and self.training:
                speech, speech_lengths = self.specaug(speech, speech_lengths)


        # Forward encoder
        encoder_out, encoder_out_lens = self.model.encoder(speech.permute(0, 2, 1), speech_lengths)
    
        return encoder_out, encoder_out_lens


    def _calc_att_loss(
            self,
            encoder_out: torch.Tensor,
            encoder_out_lens: torch.Tensor,
            ys_pad: torch.Tensor,
            ys_pad_lens: torch.Tensor,
            **kwargs,
    ):
        target_mask = kwargs.get("target_mask", None)
        stats = {}
        
        # 1. Forward decoder
        decoder_out = self.model.decoder(
            x=ys_pad, xa=encoder_out, hlens=encoder_out_lens, ys_in_lens=ys_pad_lens
        )
        
        # 2. Compute attention loss
        mask = torch.ones_like(ys_pad) * (-1)
        ys_pad_mask = (ys_pad * target_mask + mask * (1-target_mask)).to(torch.int64)
        ys_pad_mask[ys_pad_mask == 0] = -1
        loss_att = self.criterion_att(decoder_out[:, :-1, :], ys_pad_mask[:, 1:])

        with torch.no_grad():
            preds = torch.argmax(decoder_out, -1)
            acc_att = compute_accuracy(preds[:, :-1], ys_pad_mask[:, 1:], ignore_label=self.ignore_id)

        return loss_att, acc_att, None, None


    def inference(self,
                  data_in,
                  data_lengths=None,
                  key: list = None,
                  tokenizer=None,
                  frontend=None,
                  **kwargs,
                  ):
        if kwargs.get("batch_size", 1) > 1:
            raise NotImplementedError("batch decoding is not implemented")

        if frontend is None and not hasattr(self, "frontend"):
            frontend_class = tables.frontend_classes.get("WhisperFrontend")
            frontend = frontend_class(n_mels=self.model.dims.n_mels, do_pad_trim=kwargs.get("do_pad_trim", True))
            self.frontend = frontend
        else:
            frontend = frontend if frontend is not None else self.frontend

        meta_data = {}
        if isinstance(data_in, torch.Tensor) and kwargs.get("data_type", "sound") == "fbank":  # fbank
            speech, speech_lengths = data_in, data_lengths
            if len(speech.shape) < 3:
                speech = speech[None, :, :]
            if speech_lengths is None:
                speech_lengths = speech.shape[1]
        else:
            # extract fbank feats
            time1 = time.perf_counter()
            audio_sample_list = load_audio_text_image_video(data_in, fs=frontend.fs if hasattr(frontend, "fs") else 16000, audio_fs=kwargs.get("fs", 16000),
                                                            data_type=kwargs.get("data_type", "sound"),
                                                            tokenizer=tokenizer)
            time2 = time.perf_counter()
            meta_data["load_data"] = f"{time2 - time1:0.3f}"
            speech, speech_lengths = extract_fbank(audio_sample_list, data_type=kwargs.get("data_type", "sound"),
                                                   frontend=frontend)
            time3 = time.perf_counter()
            meta_data["extract_feat"] = f"{time3 - time2:0.3f}"
            frame_shift = frontend.frame_shift if hasattr(frontend, "frame_shift") else 10
            lfr_n = frontend.lfr_n if hasattr(frontend, "lfr_n") else 1
            meta_data["batch_data_time"] = speech_lengths.sum().item() * frame_shift * lfr_n / 1000

        speech = speech.to(device=kwargs["device"])[0, :, :]
        speech_lengths = speech_lengths.to(device=kwargs["device"])

        DecodingOptions = kwargs.get("DecodingOptions", {})
        task = DecodingOptions.get("task", "ASR")
        if isinstance(task, str):
            task = [task]
        task = "".join([f"<|{x}|>" for x in task])
        initial_prompt = kwargs.get("initial_prompt", f"<|startoftranscript|>{task}")
        DecodingOptions["initial_prompt"] = initial_prompt
        
        language = DecodingOptions.get("language", None)
        language = None if language == "auto" else language
        DecodingOptions["language"] = language

        DecodingOptions["vocab_path"] = kwargs.get("vocab_path", None)
        
        
        if "without_timestamps" not in DecodingOptions:
            DecodingOptions["without_timestamps"] = True

    
        options = whisper.DecodingOptions(**DecodingOptions)
        
        result = whisper.decode(self.model, speech, options)
        text = f"{result.text}"
        results = []
        result_i = {"key": key[0], "text": text}

        results.append(result_i)
    
        return results, meta_data
    