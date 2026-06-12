"""
Layered Document Service - Canvas Editor Core

This service creates and manages layered JSON documents for the Canvas Editor.
Documents contain separate layers for background, text, logo, etc.

Architecture:
- Each content draft can have a "document" field (layered JSON)
- The document is the source of truth for editing
- Final PNG is rendered from the document on-demand
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
import uuid


class LayeredDocumentService:
    """Service for creating and managing layered documents"""

    @staticmethod
    def create_document(
        canvas_width: int = 1080,
        canvas_height: int = 1080,
        aspect_ratio: str = "1:1",
        background_color: str = "#000000"
    ) -> Dict[str, Any]:
        """
        Create a new empty layered document

        Returns:
            Empty document structure ready for layers
        """
        return {
            "id": str(uuid.uuid4()),
            "version": 1,
            "canvas": {
                "width": canvas_width,
                "height": canvas_height,
                "aspect_ratio": aspect_ratio,
                "background_color": background_color
            },
            "layers": [],
            "metadata": {
                "created_at": datetime.utcnow().isoformat(),
                "generated_by": "uri_social_v3",
                "editor_version": "1.0"
            }
        }

    @staticmethod
    def add_background_layer(
        document: Dict[str, Any],
        image_url: str,
        generation_prompt: str = "",
        generation_seed: Optional[int] = None,
        locked: bool = True
    ) -> Dict[str, Any]:
        """
        Add AI-generated background layer

        Args:
            document: The document to add layer to
            image_url: URL of the generated background image
            generation_prompt: Prompt used to generate the image
            generation_seed: Random seed for reproducibility
            locked: Whether layer can be edited (backgrounds locked by default)

        Returns:
            Updated document
        """
        layer = {
            "id": f"layer_bg_{str(uuid.uuid4())[:8]}",
            "type": "ai_generated_background",
            "z_index": 0,
            "url": image_url,
            "generation_prompt": generation_prompt,
            "generation_seed": generation_seed,
            "locked": locked,
            "visible": True
        }
        document["layers"].append(layer)
        return document

    @staticmethod
    def add_text_layer(
        document: Dict[str, Any],
        content: str,
        font_family: str = "Arial",
        font_size: int = 48,
        font_weight: int = 400,
        color: str = "#FFFFFF",
        x: int = 100,
        y: int = 100,
        z_index: int = 10,
        max_width: Optional[int] = None,
        text_align: str = "left",
        line_height: float = 1.2,
        letter_spacing: float = 0,
        locked: bool = False
    ) -> Dict[str, Any]:
        """
        Add editable text layer

        Args:
            document: The document to add layer to
            content: Text content
            font_family: Font family name
            font_size: Font size in pixels
            font_weight: Font weight (400=normal, 700=bold)
            color: Text color (hex)
            x, y: Position on canvas
            z_index: Layer stacking order (higher = front)
            max_width: Maximum width for text wrapping
            text_align: Text alignment (left, center, right)
            line_height: Line height multiplier
            letter_spacing: Letter spacing in pixels
            locked: Whether layer can be edited

        Returns:
            Updated document
        """
        layer = {
            "id": f"layer_text_{str(uuid.uuid4())[:8]}",
            "type": "text",
            "z_index": z_index,
            "content": content,
            "font_family": font_family,
            "font_size": font_size,
            "font_weight": font_weight,
            "color": color,
            "x": x,
            "y": y,
            "max_width": max_width or (document["canvas"]["width"] - x - 100),
            "text_align": text_align,
            "line_height": line_height,
            "letter_spacing": letter_spacing,
            "locked": locked,
            "visible": True
        }
        document["layers"].append(layer)
        return document

    @staticmethod
    def add_logo_layer(
        document: Dict[str, Any],
        logo_url: str,
        x: int,
        y: int,
        width: int = 120,
        height: int = 60,
        z_index: int = 20,
        opacity: float = 1.0,
        locked: bool = False,
        visible: bool = True
    ) -> Dict[str, Any]:
        """
        Add brand logo layer

        Args:
            document: The document to add layer to
            logo_url: URL of the logo image
            x, y: Position on canvas
            width, height: Logo dimensions
            z_index: Layer stacking order
            opacity: Opacity (0.0 - 1.0)
            locked: Whether layer can be edited
            visible: Whether layer is visible

        Returns:
            Updated document
        """
        layer = {
            "id": f"layer_logo_{str(uuid.uuid4())[:8]}",
            "type": "brand_asset",
            "asset_role": "logo",
            "z_index": z_index,
            "url": logo_url,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "opacity": opacity,
            "locked": locked,
            "visible": visible
        }
        document["layers"].append(layer)
        return document

    @staticmethod
    def add_product_layer(
        document: Dict[str, Any],
        product_url: str,
        source_product_id: str,
        x: int,
        y: int,
        width: int,
        height: int,
        rotation: float = 0,
        shadow: Optional[Dict[str, Any]] = None,
        z_index: int = 1,
        locked: bool = True
    ) -> Dict[str, Any]:
        """
        Add composited product layer

        Args:
            document: The document to add layer to
            product_url: URL of the product cutout image
            source_product_id: ID of the original product
            x, y: Position on canvas
            width, height: Product dimensions
            rotation: Rotation in degrees
            shadow: Shadow effect config {color, opacity, blur, offset_y}
            z_index: Layer stacking order
            locked: Whether layer can be edited

        Returns:
            Updated document
        """
        layer = {
            "id": f"layer_product_{str(uuid.uuid4())[:8]}",
            "type": "composited_product",
            "z_index": z_index,
            "url": product_url,
            "source_product_id": source_product_id,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "rotation": rotation,
            "shadow": shadow or {
                "color": "#000000",
                "opacity": 0.4,
                "blur": 24,
                "offset_y": 8
            },
            "locked": locked,
            "visible": True
        }
        document["layers"].append(layer)
        return document

    @staticmethod
    def get_layer_by_id(document: Dict[str, Any], layer_id: str) -> Optional[Dict[str, Any]]:
        """Find a layer by its ID"""
        for layer in document.get("layers", []):
            if layer.get("id") == layer_id:
                return layer
        return None

    @staticmethod
    def update_layer(
        document: Dict[str, Any],
        layer_id: str,
        updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update specific layer properties

        Args:
            document: The document containing the layer
            layer_id: ID of the layer to update
            updates: Dictionary of properties to update

        Returns:
            Updated document
        """
        for layer in document.get("layers", []):
            if layer.get("id") == layer_id:
                layer.update(updates)
                break
        return document

    @staticmethod
    def delete_layer(document: Dict[str, Any], layer_id: str) -> Dict[str, Any]:
        """
        Delete a layer by ID

        Args:
            document: The document containing the layer
            layer_id: ID of the layer to delete

        Returns:
            Updated document
        """
        document["layers"] = [
            layer for layer in document.get("layers", [])
            if layer.get("id") != layer_id
        ]
        return document

    @staticmethod
    def reorder_layers(
        document: Dict[str, Any],
        layer_order: List[str]
    ) -> Dict[str, Any]:
        """
        Reorder layers by ID list

        Args:
            document: The document to reorder
            layer_order: List of layer IDs in desired order (front to back)

        Returns:
            Updated document with layers reordered
        """
        layer_map = {layer["id"]: layer for layer in document.get("layers", [])}
        reordered = []

        for i, layer_id in enumerate(layer_order):
            if layer_id in layer_map:
                layer = layer_map[layer_id]
                layer["z_index"] = len(layer_order) - i  # Reverse index
                reordered.append(layer)

        document["layers"] = sorted(reordered, key=lambda l: l.get("z_index", 0))
        return document

    @staticmethod
    def get_sorted_layers(document: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Get layers sorted by z_index (bottom to top)

        Returns:
            List of layers sorted for rendering
        """
        return sorted(
            document.get("layers", []),
            key=lambda l: l.get("z_index", 0)
        )

    @staticmethod
    def clone_document(document: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a deep copy of a document

        Returns:
            Cloned document with new ID
        """
        import copy
        cloned = copy.deepcopy(document)
        cloned["id"] = str(uuid.uuid4())
        cloned["metadata"]["cloned_at"] = datetime.utcnow().isoformat()
        return cloned

    @staticmethod
    def increment_version(document: Dict[str, Any]) -> Dict[str, Any]:
        """Increment document version number"""
        document["version"] = document.get("version", 1) + 1
        document["metadata"]["updated_at"] = datetime.utcnow().isoformat()
        return document
