# SPDX-License-Identifier: Apache-2.0
# Derived from JKc66/CircuitVision, commit fc5b775043b577668906b6d0bbb238a690a82965.
# Original source and modifications are documented in the adjacent NOTICE.
from ultralytics import YOLO
import cv2
import numpy as np
import torch
from PIL import Image
from .sam2_infer import SAM2Transforms, get_modified_sam2

class CircuitAnalyzer:
    """Local-only subset of CircuitVision; never infers electrical connectivity."""

    def __init__(self, yolo_path, sam2_config_path, sam2_base_checkpoint_path,
                 sam2_finetuned_checkpoint_path, use_sam2=True, debug=False):
        self.yolo = YOLO(str(yolo_path))
        self.yolo.to(torch.device("cpu"))
        self.debug = debug
        self.use_sam2 = use_sam2
        self.sam2_model = None
        self.sam2_transforms = None
        self.sam2_device = torch.device("cpu")
        self.last_sam2_output = None
        if not use_sam2:
            return
        base_parts = [
            "sam_mask_decoder.transformer.layers.0.self_attn.k_proj",
            "sam_mask_decoder.transformer.layers.0.self_attn.q_proj",
            "sam_mask_decoder.transformer.layers.0.self_attn.v_proj",
            "sam_mask_decoder.transformer.layers.0.self_attn.out_proj",
            "sam_mask_decoder.transformer.layers.1.self_attn.k_proj",
            "sam_mask_decoder.transformer.layers.1.self_attn.q_proj",
            "sam_mask_decoder.transformer.layers.1.self_attn.v_proj",
            "sam_mask_decoder.transformer.layers.1.self_attn.out_proj",
            "sam_mask_decoder.transformer.layers.0.cross_attn_token_to_image.k_proj",
            "sam_mask_decoder.transformer.layers.0.cross_attn_token_to_image.q_proj",
            "sam_mask_decoder.transformer.layers.0.cross_attn_token_to_image.v_proj",
            "sam_mask_decoder.transformer.layers.0.cross_attn_token_to_image.out_proj",
            "sam_mask_decoder.transformer.layers.1.cross_attn_token_to_image.k_proj",
            "sam_mask_decoder.transformer.layers.1.cross_attn_token_to_image.q_proj",
            "sam_mask_decoder.transformer.layers.1.cross_attn_token_to_image.v_proj",
            "sam_mask_decoder.transformer.layers.1.cross_attn_token_to_image.out_proj",
            "sam_mask_decoder.transformer.layers.0.mlp.layers.0",
            "sam_mask_decoder.transformer.layers.0.mlp.layers.1",
            "sam_mask_decoder.transformer.layers.1.mlp.layers.0",
            "sam_mask_decoder.transformer.layers.1.mlp.layers.1"
            ]
        added_parts = [
            "sam_mask_decoder.iou_prediction_head.layers.2",
            "sam_mask_decoder.conv_s0",
            "sam_mask_decoder.conv_s1",

            "image_encoder.neck.convs.2.conv",
            "image_encoder.neck.convs.3.conv", 

            "image_encoder.trunk.blocks.44.attn.qkv", # for downsampling
            "image_encoder.trunk.blocks.44.mlp.layers.0",
            "image_encoder.trunk.blocks.44.proj",

            "image_encoder.trunk.blocks.47.attn.qkv",
            "image_encoder.trunk.blocks.47.mlp.layers.0",

            "sam_mask_decoder.transformer.layers.0.cross_attn_image_to_token.q_proj",
            "sam_mask_decoder.transformer.layers.0.cross_attn_image_to_token.k_proj",
            "sam_mask_decoder.transformer.layers.0.cross_attn_image_to_token.v_proj",
            "sam_mask_decoder.transformer.layers.1.cross_attn_image_to_token.q_proj",
            "sam_mask_decoder.transformer.layers.1.cross_attn_image_to_token.k_proj",
            "sam_mask_decoder.transformer.layers.1.cross_attn_image_to_token.v_proj",
            ]

        # Build Model Structure using the provided paths
        print(f"Building SAM2 model with config: {sam2_config_path} and base checkpoint: {sam2_base_checkpoint_path}")
        self.sam2_model = get_modified_sam2(
            model_cfg_path=sam2_config_path,
            checkpoint_path=str(sam2_base_checkpoint_path),
            device=str(self.sam2_device),
            use_high_res_features=True,
            use_peft=True,
            lora_rank=4,
            lora_alpha=16,
            lora_dropout=0.3,
            lora_target_modules=base_parts + added_parts,
            use_wrapper=True,
            trainable_embedding_r=4,
            use_refinement_layer=True,
            refinement_kernels=[3, 5, 7, 11],
            kernel_channels=2,
            weight_dice=0.5, 
            weight_focal=0.4, 
            weight_iou=0.3, 
            weight_freq=0.1,
            focal_alpha=0.25
        )

        # Load the fine-tuned weights
        print(f"Loading SAM2 fine-tuned weights from: {sam2_finetuned_checkpoint_path}")
        checkpoint = torch.load(str(sam2_finetuned_checkpoint_path), map_location=self.sam2_device, weights_only=True)
        if 'state_dict' in checkpoint:
            model_state_dict = checkpoint['state_dict']
        else:
            model_state_dict = checkpoint

        self.sam2_model.load_state_dict(model_state_dict)
        self.sam2_model.eval()  # Set to evaluation mode

        # Initialize transforms
        if hasattr(self.sam2_model, 'sam2_model') and hasattr(self.sam2_model.sam2_model, 'image_size'):
            resolution = self.sam2_model.sam2_model.image_size
        elif hasattr(self.sam2_model, 'image_size'):
            resolution = self.sam2_model.image_size
        else:
            resolution = 1024  # Default fallback
            print(f"Warning: Could not determine SAM2 model image size, using default: {resolution}")

        self.sam2_transforms = SAM2Transforms(
            resolution=resolution,
            mask_threshold=0,
            max_hole_area=0,
            max_sprinkle_area=0
        )

        print("SAM2 Model loaded successfully")
        print("--- SAM2 Ready ---")

    def bboxes(self, image):
            results = self.yolo.predict(image, verbose=True, device="cpu")
            results = results[0]
            img_classes_ids = results.boxes.cls.cpu().numpy().tolist() # Get numeric class IDs
            img_classes_names = [results.names[int(num_id)] for num_id in img_classes_ids] # Get string names
            confidences = results.boxes.conf.cpu().numpy().tolist()
            boxes_coords = results.boxes.xyxy.cpu().numpy().tolist()
        
            processed_boxes = []
            for i, (xmin, ymin, xmax, ymax) in enumerate(boxes_coords):
                processed_boxes.append({
                    'class': img_classes_names[i],
                    '_yolo_class_id_temp': int(img_classes_ids[i]), # Store numeric ID temporarily
                    'confidence': confidences[i],
                    'xmin': round(xmin),
                    'ymin': round(ymin),
                    'xmax': round(xmax),
                    'ymax': round(ymax),
                    'persistent_uid': f"{img_classes_names[i]}_{round(xmin)}_{round(ymin)}_{round(xmax)}_{round(ymax)}"
                })
            return processed_boxes

    def segment_with_sam2(self, image_np_bgr):
            """
            Segments the circuit diagram using the loaded SAM 2 model.
            Also computes a bounding box encompassing all detected SAM2 mask contours.

            Args:
                image_np_bgr (np.ndarray): Input image in BGR format (from cv2.imread).

            Returns:
                Tuple[np.ndarray | None, np.ndarray | None, Tuple[int,int,int,int] | None]:
                    - mask_binary_np: Binary mask (uint8, 0 or 255) from SAM2, at original image resolution.
                    - self.last_sam2_output: Colored version of the mask for display.
                    - sam_extent_bbox: Tuple (xmin, ymin, xmax, ymax) of the bounding box around SAM2 contours, or None.
            """
            if not self.use_sam2 or self.sam2_model is None or self.sam2_transforms is None:
                print("SAM 2 is not available or not initialized. Cannot segment.")
                self.last_sam2_output = None
                return None, None, None

            if self.debug:
                print("Segmenting with SAM 2...")
            try:
                image_np_rgb = cv2.cvtColor(image_np_bgr, cv2.COLOR_BGR2RGB)
                image_pil = Image.fromarray(image_np_rgb)
                orig_hw = image_np_rgb.shape[:2]

                image_tensor = self.sam2_transforms(image_pil).unsqueeze(0).to(self.sam2_device)

                self.sam2_model.eval()
                with torch.no_grad():
                    high_res_mask, low_res_mask, _ = self.sam2_model(image_tensor)
                    chosen_mask = high_res_mask

                final_mask_tensor = self.sam2_transforms.postprocess_masks(chosen_mask, orig_hw)
                mask_squeezed = final_mask_tensor.detach().cpu().squeeze()
                mask_binary_np = (mask_squeezed > 0.0).numpy().astype(np.uint8) * 255

                # Store colored version for display
                colored_output = cv2.cvtColor(mask_binary_np, cv2.COLOR_GRAY2BGR)
                colored_output[:,:,0] = 0
                colored_output[:,:,2] = 0
                self.last_sam2_output = colored_output

                # --- Calculate bounding box of SAM2 mask contours ---
                sam_extent_bbox = None
                contours_sam, _ = cv2.findContours(mask_binary_np, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if contours_sam:
                    all_points = np.concatenate(contours_sam)
                    x_sam, y_sam, w_sam, h_sam = cv2.boundingRect(all_points)
                    sam_extent_bbox = (x_sam, y_sam, x_sam + w_sam, y_sam + h_sam)
                    if self.debug:
                        print(f"SAM2 Extent BBox calculated: {sam_extent_bbox}")
                elif self.debug:
                    print("No contours found in SAM2 mask to calculate extent bbox.")
                # --- End BBox Calculation ---

                if self.debug:
                    print("SAM 2 Segmentation successful.")
                return mask_binary_np, self.last_sam2_output, sam_extent_bbox

            except Exception as e:
                print(f"Error during SAM 2 segmentation: {e}")
                import traceback
                print(traceback.format_exc())
                self.last_sam2_output = None
                return None, None, None
