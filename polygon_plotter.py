import streamlit as st
import json
from shapely import wkt
from shapely.geometry import mapping, shape
from shapely.ops import transform
from pyproj import Transformer
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium

st.set_page_config(layout="wide")

LAYER_COLORS = [
    "#e63946", "#1d3557", "#2a9d8f", "#f4a261", "#8d99ae",
    "#457b9d", "#ff006e", "#6a994e", "#bc6c25", "#3a86ff"
]

UK_CRS_OPTIONS = [
    ("WGS 84 (EPSG:4326)", "EPSG:4326"),
    ("British National Grid (EPSG:27700)", "EPSG:27700"),
    ("Web Mercator (EPSG:3857)", "EPSG:3857"),
    ("ETRS89 (EPSG:4258)", "EPSG:4258"),
    ("ETRS89 / UTM zone 30N (EPSG:25830)", "EPSG:25830"),
    ("ETRS89 / UTM zone 31N (EPSG:25831)", "EPSG:25831"),
    ("WGS 84 / UTM zone 30N (EPSG:32630)", "EPSG:32630"),
    ("WGS 84 / UTM zone 31N (EPSG:32631)", "EPSG:32631"),
    ("TM65 / Irish Grid (EPSG:29902)", "EPSG:29902"),
    ("IRENET95 / Irish Transverse Mercator (EPSG:2157)", "EPSG:2157"),
]

CRS_LABEL_TO_CODE = dict(UK_CRS_OPTIONS)
CRS_PLACEHOLDER = "Select CRS"


def parse_geometry_input(geom_input: str):
    """Parse pasted geometry text as GeoJSON first, then WKT fallback."""
    text = (geom_input or "").strip()
    if not text:
        raise ValueError("Geometry input is empty.")

    # Try GeoJSON if the payload looks JSON-like.
    if text[0] in "[{":
        try:
            payload = json.loads(text)

            if isinstance(payload, dict) and payload.get("type") == "Feature":
                geom_obj = payload.get("geometry")
                if geom_obj is None:
                    raise ValueError("GeoJSON Feature is missing 'geometry'.")
                return shape(geom_obj), "GeoJSON"

            if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
                features = payload.get("features") or []
                if len(features) != 1:
                    raise ValueError("FeatureCollection must contain exactly one feature.")
                geom_obj = features[0].get("geometry")
                if geom_obj is None:
                    raise ValueError("GeoJSON Feature is missing 'geometry'.")
                return shape(geom_obj), "GeoJSON"

            return shape(payload), "GeoJSON"
        except Exception:
            # Fallback to WKT parser when JSON parsing/shape construction fails.
            pass

    return wkt.loads(text), "WKT"


def transform_geometry_crs(geom, src_crs: str, dst_crs: str):
    if src_crs == dst_crs:
        return geom
    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return transform(transformer.transform, geom)


def build_sql_ctas_script(geometry_text: str, geometry_format: str, crs_code: str) -> str:
    epsg_code = "NULL"
    if isinstance(crs_code, str) and ":" in crs_code:
        epsg_code = crs_code.split(":", 1)[1]

    escaped_geometry = geometry_text.replace("'", "''")
    if geometry_format == "GeoJSON":
        geom_expr = f"ST_SetSRID(ST_GeomFromGeoJSON('{escaped_geometry}'), {epsg_code})"
    else:
        geom_expr = f"ST_GeomFromText('{escaped_geometry}', {epsg_code})"

    return f"""-- Auto-generated from polygon_plotter.py
-- Review SQL function names for your SQL engine if needed.

CREATE OR REPLACE TABLE USER_DRAWN_GEOMETRY AS
SELECT
    1 AS id,
    {geom_expr} AS geometry,
    '{crs_code}' AS crs;
"""


def build_python_geopandas_script(geometry_text: str, geometry_format: str, crs_code: str) -> str:
    return f"""# Auto-generated from polygon_plotter.py
import json
import geopandas as gpd
from shapely import wkt
from shapely.geometry import shape

GEOMETRY_TEXT = {geometry_text!r}
GEOMETRY_FORMAT = {geometry_format!r}
CRS = {crs_code!r}

if GEOMETRY_FORMAT == "WKT":
    geometry = wkt.loads(GEOMETRY_TEXT)
else:
    geometry = shape(json.loads(GEOMETRY_TEXT))

gdf = gpd.GeoDataFrame(
    data={{"id": [1]}},
    geometry=[geometry],
    crs=CRS,
)

print(gdf)
print("CRS:", gdf.crs)
"""


def build_snowflake_sql_script(geometry_text: str, geometry_format: str, crs_code: str) -> str:
    epsg_code = "NULL"
    if isinstance(crs_code, str) and ":" in crs_code:
        epsg_code = crs_code.split(":", 1)[1]

    escaped_geometry = geometry_text.replace("'", "''")
    if geometry_format == "GeoJSON":
        geom_expr = f"ST_GEOMFROMGEOJSON('{escaped_geometry}')"
    else:
        geom_expr = f"ST_GEOMFROMTEXT('{escaped_geometry}')"

    return f"""-- Auto-generated from polygon_plotter.py
-- Snowflake SQL for geometry import
-- Note: Snowflake geometry functions may not support SRID directly.
-- You may need to adjust the geometry handling based on your Snowflake version.

CREATE OR REPLACE TABLE user_drawn_geometry AS
SELECT
    1 AS id,
    {geom_expr} AS geometry,
    '{crs_code}' AS crs;
"""

# --- Session State ---
if "layers" not in st.session_state:
    st.session_state.layers = []
if "jump_to_layer" not in st.session_state:
    st.session_state.jump_to_layer = None
if "jump_processed" not in st.session_state:
    st.session_state.jump_processed = False

tab_plotter, tab_draw = st.tabs(["Tab 1: Plotter", "Tab 2: Draw and Export"])

with tab_plotter:
    # --- Layout: 20 / 80 split ---
    left, right = st.columns([2, 8])

    # ---------------- LEFT PANEL ----------------
    with left:
        st.header("Add Geometry Layer")

        selected_crs_label = st.selectbox(
            "CRS",
            options=[CRS_PLACEHOLDER] + [label for label, _code in UK_CRS_OPTIONS],
            index=0,
            key="plotter_crs",
        )
        crs_input = CRS_LABEL_TO_CODE.get(selected_crs_label)
        geom_input = st.text_area(
            "Geometry (WKT or GeoJSON)",
            placeholder='POINT(-0.1276 51.5074) or {"type":"Point","coordinates":[-0.1276,51.5074]}',
            height=420,
        )

        if st.button("Submit"):
            try:
                if not crs_input:
                    raise ValueError("Please select a CRS before submitting geometry.")

                geom, detected_format = parse_geometry_input(geom_input)

                # Transform to EPSG:4326 for bounds calculation
                geom_4326 = transform_geometry_crs(geom, crs_input, "EPSG:4326")
                minx, miny, maxx, maxy = geom_4326.bounds

                # Store layer
                layer_idx = len(st.session_state.layers)
                st.session_state.layers.append({
                    "crs": crs_input,
                    "geometry": geom,
                    "enabled": True,
                    "name": f"Layer {layer_idx+1}",
                    "color": LAYER_COLORS[layer_idx % len(LAYER_COLORS)],
                    "bounds": [minx, miny, maxx, maxy]
                })

                # Reset jump state when a new layer is added
                st.session_state.jump_to_layer = None
                st.session_state.jump_processed = False

                st.success(f"Layer added! Detected format: {detected_format}")

            except Exception as e:
                st.error(
                    "Invalid geometry. Paste either valid WKT or valid GeoJSON (Geometry or Feature). "
                    f"Details: {e}"
                )

        st.divider()
        st.subheader("Layers")

        # Toggle layers and add jump buttons
        for i, layer in enumerate(st.session_state.layers):
            col1, col2 = st.columns([3, 1])
            with col1:
                layer["enabled"] = st.checkbox(
                    layer["name"],
                    value=layer["enabled"],
                    key=f"layer_{i}"
                )
            with col2:
                if st.button("Jump to", key=f"jump_btn_{i}"):
                    st.session_state.jump_to_layer = i
                    st.session_state.jump_processed = False

    # ---------------- RIGHT PANEL ----------------
    with right:
        st.header("Map")

        # Base map centered roughly UK
        m = folium.Map(location=[54, -2], zoom_start=5)
        combined_bounds = None

        for i, layer in enumerate(st.session_state.layers):
            if not layer["enabled"]:
                continue

            geom = layer["geometry"]
            crs = layer["crs"]

            try:
                if not crs:
                    st.warning(f"Skipping {layer['name']}: no CRS selected for this layer.")
                    continue

                # Transform to EPSG:4326 if needed
                geom = transform_geometry_crs(geom, crs, "EPSG:4326")

                minx, miny, maxx, maxy = geom.bounds
                if combined_bounds is None:
                    combined_bounds = [minx, miny, maxx, maxy]
                else:
                    combined_bounds[0] = min(combined_bounds[0], minx)
                    combined_bounds[1] = min(combined_bounds[1], miny)
                    combined_bounds[2] = max(combined_bounds[2], maxx)
                    combined_bounds[3] = max(combined_bounds[3], maxy)

                color = layer.get("color", LAYER_COLORS[i % len(LAYER_COLORS)])
                if geom.geom_type == "Point":
                    folium.CircleMarker(
                        location=[geom.y, geom.x],
                        radius=6,
                        color=color,
                        fill=True,
                        fill_color=color,
                        fill_opacity=0.8,
                        weight=2,
                        popup=folium.Popup(layer["name"], max_width=250),
                    ).add_to(m)
                elif geom.geom_type == "MultiPoint":
                    for point in geom.geoms:
                        folium.CircleMarker(
                            location=[point.y, point.x],
                            radius=6,
                            color=color,
                            fill=True,
                            fill_color=color,
                            fill_opacity=0.8,
                            weight=2,
                            popup=folium.Popup(layer["name"], max_width=250),
                        ).add_to(m)
                else:
                    feature = {
                        "type": "Feature",
                        "properties": {"layer_name": layer["name"]},
                        "geometry": mapping(geom),
                    }
                    geojson = folium.GeoJson(
                        data=feature,
                        style_function=lambda _feature, c=color: {
                            "color": c,
                            "weight": 3,
                            "fillColor": c,
                            "fillOpacity": 0.25,
                        },
                        popup=folium.GeoJsonPopup(
                            fields=["layer_name"],
                            aliases=["Layer"],
                            labels=False,
                        ),
                    )
                    geojson.add_to(m)

            except Exception as e:
                st.warning(f"Error rendering {layer['name']}: {e}")

        # Check if user clicked jump to a specific layer (takes priority)
        jump_just_completed = False
        if st.session_state.jump_to_layer is not None and not st.session_state.jump_processed:
            jump_idx = st.session_state.jump_to_layer
            if 0 <= jump_idx < len(st.session_state.layers):
                layer = st.session_state.layers[jump_idx]
                bounds = layer.get("bounds")
                if bounds:
                    minx, miny, maxx, maxy = bounds
                    if minx == maxx and miny == maxy:
                        delta = 0.01
                        minx -= delta
                        maxx += delta
                        miny -= delta
                        maxy += delta
                    m.fit_bounds([[miny, minx], [maxy, maxx]])
                    jump_just_completed = True
            st.session_state.jump_to_layer = None
            st.session_state.jump_processed = False

        if not jump_just_completed and combined_bounds is not None:
            # Default: fit to all enabled layers
            west, south, east, north = combined_bounds
            if west == east and south == north:
                delta = 0.01
                west -= delta
                east += delta
                south -= delta
                north += delta
            m.fit_bounds([[south, west], [north, east]])

        st_folium(m, use_container_width=True, height=700)

with tab_draw:
    st.header("Draw Polygon and Export")

    # Match Tab 1 layout (20 / 80 split)
    left, right = st.columns([2, 8])

    with left:
        target_crs_label = st.selectbox(
            "Output CRS",
            options=[CRS_PLACEHOLDER] + [label for label, _code in UK_CRS_OPTIONS],
            index=0,
            key="draw_export_crs",
        )
        target_crs = CRS_LABEL_TO_CODE.get(target_crs_label)

        output_format = st.radio(
            "Output format",
            options=["WKT", "GeoJSON"],
            horizontal=True,
            key="draw_export_format",
        )

        st.caption("Draw a polygon on the map. The latest drawn polygon is exported below.")

    with right:
        draw_map = folium.Map(location=[54, -2], zoom_start=6)
        Draw(
            export=False,
            draw_options={
                "polyline": False,
                "marker": False,
                "circle": False,
                "circlemarker": False,
                "rectangle": False,
                "polygon": True,
            },
            edit_options={"edit": True, "remove": True},
        ).add_to(draw_map)

        draw_data = st_folium(
            draw_map,
            use_container_width=True,
            height=650,
            key="draw_map",
        )

    drawn_items = (draw_data or {}).get("all_drawings") or []

    if drawn_items:
        last_item = drawn_items[-1]
        geometry_payload = last_item.get("geometry") if isinstance(last_item, dict) else None

        if geometry_payload:
            try:
                drawn_geom = shape(geometry_payload)
                if drawn_geom.geom_type not in {"Polygon", "MultiPolygon"}:
                    with left:
                        st.warning("Please draw a polygon geometry.")
                else:
                    if not target_crs:
                        with left:
                            st.warning("Please select an output CRS.")
                    else:
                        output_geom = transform_geometry_crs(drawn_geom, "EPSG:4326", target_crs)

                        if output_format == "WKT":
                            output_text = output_geom.wkt
                        else:
                            output_text = json.dumps(mapping(output_geom), indent=2)

                        with left:
                            st.subheader("Exported Geometry")
                            st.caption(f"Format: {output_format} | CRS: {target_crs}")
                            st.text_area(
                                "Exported geometry",
                                value=output_text,
                                height=340,
                                key=f"export_geometry_text_{output_format}_{target_crs}",
                                disabled=True,
                            )

                            sql_script = build_sql_ctas_script(output_text, output_format, target_crs)
                            snowflake_script = build_snowflake_sql_script(output_text, output_format, target_crs)
                            python_script = build_python_geopandas_script(output_text, output_format, target_crs)

                            sql_col, sf_col, py_col = st.columns(3)
                            with sql_col:
                                st.download_button(
                                    label="Download SQL CTAS",
                                    data=sql_script,
                                    file_name="drawn_geometry_ctas.sql",
                                    mime="text/sql",
                                )
                            with sf_col:
                                st.download_button(
                                    label="Download Snowflake SQL",
                                    data=snowflake_script,
                                    file_name="drawn_geometry_snowflake.sql",
                                    mime="text/sql",
                                )
                            with py_col:
                                st.download_button(
                                    label="Download Python GeoPandas",
                                    data=python_script,
                                    file_name="drawn_geometry_geopandas.py",
                                    mime="text/x-python",
                                )
            except Exception as e:
                with left:
                    st.error(f"Could not export drawn geometry: {e}")
    else:
        with left:
            st.info("No polygon drawn yet. Use the polygon tool on the map to create one.")