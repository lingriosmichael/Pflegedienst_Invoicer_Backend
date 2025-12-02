"""
Dashboard management for persisting graphs and visualizations.
"""
import json
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any
from app.db.connection import get_db
from app.core.logging import get_logger

logger = get_logger(__name__)

def init_dashboard_db():
    """Initialize dashboard storage table."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_widgets (
            id TEXT PRIMARY KEY,
            title TEXT,
            chart_type TEXT,
            chart_data TEXT,
            chart_config TEXT,
            position_x INTEGER,
            position_y INTEGER,
            width INTEGER,
            height INTEGER,
            created_at TEXT,
            updated_at TEXT
        )
        """)
        conn.commit()

def add_widget(title: str, chart_type: str, chart_data: Dict[str, Any], 
               chart_config: Dict[str, Any], x: int = 0, y: int = 0,
               width: int = 400, height: int = 300) -> str:
    """Add a new widget to dashboard."""
    widget_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
        INSERT INTO dashboard_widgets 
        (id, title, chart_type, chart_data, chart_config, position_x, position_y, width, height, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            widget_id, title, chart_type, 
            json.dumps(chart_data), json.dumps(chart_config),
            x, y, width, height, now, now
        ))
        conn.commit()
    
    logger.info(f"Added dashboard widget: {widget_id}")
    return widget_id

def update_widget_position(widget_id: str, x: int, y: int, width: int = None, height: int = None):
    """Update widget position and size."""
    with get_db() as conn:
        c = conn.cursor()
        now = datetime.now().isoformat()
        
        if width is not None and height is not None:
            c.execute("""
            UPDATE dashboard_widgets 
            SET position_x = ?, position_y = ?, width = ?, height = ?, updated_at = ?
            WHERE id = ?
            """, (x, y, width, height, now, widget_id))
        else:
            c.execute("""
            UPDATE dashboard_widgets 
            SET position_x = ?, position_y = ?, updated_at = ?
            WHERE id = ?
            """, (x, y, now, widget_id))
        
        conn.commit()
    logger.info(f"Updated widget position: {widget_id}")

def delete_widget(widget_id: str):
    """Delete a widget from dashboard."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM dashboard_widgets WHERE id = ?", (widget_id,))
        conn.commit()
    logger.info(f"Deleted widget: {widget_id}")

def get_all_widgets() -> List[Dict[str, Any]]:
    """Get all dashboard widgets."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
        SELECT id, title, chart_type, chart_data, chart_config, 
               position_x, position_y, width, height, created_at, updated_at
        FROM dashboard_widgets
        ORDER BY created_at DESC
        """)
        rows = c.fetchall()
        
        widgets = []
        for row in rows:
            widgets.append({
                "id": row[0],
                "title": row[1],
                "chart_type": row[2],
                "chart_data": json.loads(row[3]),
                "chart_config": json.loads(row[4]),
                "position_x": row[5],
                "position_y": row[6],
                "width": row[7],
                "height": row[8],
                "created_at": row[9],
                "updated_at": row[10]
            })
        
        return widgets

def get_widget(widget_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific widget."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
        SELECT id, title, chart_type, chart_data, chart_config, 
               position_x, position_y, width, height, created_at, updated_at
        FROM dashboard_widgets
        WHERE id = ?
        """, (widget_id,))
        row = c.fetchone()
        
        if not row:
            return None
        
        return {
            "id": row[0],
            "title": row[1],
            "chart_type": row[2],
            "chart_data": json.loads(row[3]),
            "chart_config": json.loads(row[4]),
            "position_x": row[5],
            "position_y": row[6],
            "width": row[7],
            "height": row[8],
            "created_at": row[9],
            "updated_at": row[10]
        }

def clear_dashboard():
    """Clear all dashboard widgets."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM dashboard_widgets")
        conn.commit()
    logger.info("Dashboard cleared")
