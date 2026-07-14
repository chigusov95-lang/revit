# -*- coding: utf-8 -*-
"""
Инструмент «Пленки» — раскладка цветовых областей на щите,
марки и размерные линии для Revit (pyRevit).
"""
import json
import math
import os

import clr

clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")

from System.Collections.Generic import List
from System.Windows import SystemParameters, WindowStartupLocation

from Autodesk.Revit.DB import (
    BoundingBoxIntersectsFilter,
    BuiltInCategory,
    BuiltInParameter,
    CurveLoop,
    DetailCurve,
    Dimension,
    DimensionType,
    Element,
    ElementId,
    FilteredElementCollector,
    FilledRegion,
    FilledRegionType,
    IndependentTag,
    Line,
    Options,
    Outline,
    Plane,
    Reference,
    ReferenceArray,
    SketchPlane,
    StorageType,
    TagMode,
    TagOrientation,
    Transaction,
    UnitTypeId,
    UnitUtils,
    XYZ,
)
from Autodesk.Revit.Exceptions import OperationCanceledException

from pyrevit import forms, revit


doc = revit.doc
uidoc = revit.uidoc

# --- Константы ---

MIN_OVERLAP_MM = 150
MAX_OFFSET_MM = 2000.0
MAX_SHEET_COUNT = 200
DIMENSION_TYPE_NAME = u"_AVGST_Линейный_8. Без выносной линии"

# Размерные отступы в «бумажных» мм × масштаб вида
DIM_WITNESS_1_PAPER_MM = 6.0
DIM_WITNESS_2_PAPER_MM = 16.0
DIM_TICK_LEN_PAPER_MM = 1.5
DIM_STAGGER_PAPER_MM = 5.0

# (ключ в имени типа, ширина рулона мм, префикс марки)
_ROLL_RULES = (
    (u"ветрозащита", 1500, u"ВЗ"),
    (u"пароизоляция", 3000, u"ПИ"),
    (u"сетка", 1000, u"СГ"),
)

_POSITION_FILE = os.path.join(os.path.dirname(__file__), "window_position.json")


# --- Мелкие утилиты ---

def _is_finite_positive(value):
    return value == value and value not in (float("inf"), float("-inf")) and value > 0


def _load_window_position():
    try:
        with open(_POSITION_FILE, "r") as f:
            data = json.load(f)
        return float(data["left"]), float(data["top"])
    except Exception:
        return None, None


def _save_window_position(left, top):
    try:
        with open(_POSITION_FILE, "w") as f:
            json.dump({"left": left, "top": top}, f)
    except Exception:
        pass


def _clamp_to_screen(left, top):
    min_left = SystemParameters.VirtualScreenLeft
    min_top = SystemParameters.VirtualScreenTop
    max_left = min_left + SystemParameters.VirtualScreenWidth - 100
    max_top = min_top + SystemParameters.VirtualScreenHeight - 100
    return max(min_left, min(left, max_left)), max(min_top, min(top, max_top))


def _get_type_name(element):
    return Element.Name.GetValue(element)


def _mm_to_feet(value_mm):
    return UnitUtils.ConvertToInternalUnits(value_mm, UnitTypeId.Millimeters)


def _feet_to_mm(value_ft):
    return UnitUtils.ConvertFromInternalUnits(value_ft, UnitTypeId.Millimeters)


def _parse_float(text, default=None):
    try:
        return float(text.strip().replace(",", "."))
    except Exception:
        return default


def _parse_offset_mm(text):
    value = _parse_float(text)
    if value is None or value < 0 or value > MAX_OFFSET_MM:
        return None
    return value


def _get_combo_text(combo):
    item = combo.SelectedItem
    if item is None:
        return None
    return item.Content if hasattr(item, "Content") else item


def _match_roll_rule(type_name):
    """Подбор ширины рулона и префикса марки по имени типа пленки."""
    if not type_name:
        return None
    name_lower = type_name.lower()
    for keyword, width_mm, mark_prefix in _ROLL_RULES:
        if keyword in name_lower:
            return width_mm, mark_prefix
    return None


def _resolve_roll_width_mm(type_name):
    rule = _match_roll_rule(type_name)
    return rule[0] if rule else 1500


def _mark_prefix_from_type(type_name):
    rule = _match_roll_rule(type_name)
    if rule:
        return rule[1]
    stripped = (type_name or u"").strip()
    return stripped[:3] if len(stripped) >= 3 else (stripped or u"П")


def _find_element_type(doc, element_class, name):
    collector = FilteredElementCollector(doc).OfClass(element_class)
    try:
        for element_type in collector:
            try:
                if _get_type_name(element_type) == name:
                    return element_type
            except Exception:
                continue
    finally:
        collector.Dispose()
    return None


# --- Параметр «Марка» ---

def _get_mark_param(element, for_write=False):
    param = element.LookupParameter(u"Марка")
    if param is None or (for_write and param.IsReadOnly):
        try:
            param = element.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)
        except Exception:
            param = None
    if param is None or (for_write and param.IsReadOnly):
        return None
    return param


def _read_mark_parameter(element):
    param = _get_mark_param(element)
    if param is None:
        return None
    try:
        value = param.AsString()
    except Exception:
        value = None
    return value if value else None


def _set_mark_parameter(element, mark_value):
    if not mark_value or element is None:
        return False
    param = _get_mark_param(element, for_write=True)
    if param is None:
        return False
    try:
        if param.StorageType == StorageType.String:
            param.Set(mark_value)
        else:
            param.SetValueString(mark_value)
        return True
    except Exception:
        return False


# --- Геометрия вида и щита ---

def _ensure_work_plane(doc, view):
    """PickPoint требует рабочую плоскость вида."""
    try:
        if view.SketchPlane is not None:
            return
    except Exception:
        pass

    plane = Plane.CreateByNormalAndOrigin(view.ViewDirection, view.Origin)
    transaction = Transaction(doc, u"Назначение рабочей плоскости")
    transaction.Start()
    try:
        view.SketchPlane = SketchPlane.Create(doc, plane)
        transaction.Commit()
    except Exception:
        transaction.RollBack()
        raise


def _get_local_bounds(p1, p2, view):
    """Две точки щита → прямоугольник в осях вида (Right / Up)."""
    right = view.RightDirection
    up = view.UpDirection
    origin = p1
    base_r = origin.DotProduct(right)
    base_u = origin.DotProduct(up)

    diff = p2 - p1
    delta_r = diff.DotProduct(right)
    delta_u = diff.DotProduct(up)
    corners = [p1, p1 + right * delta_r, p2, p1 + up * delta_u]

    local_rs = [c.DotProduct(right) - base_r for c in corners]
    local_us = [c.DotProduct(up) - base_u for c in corners]
    return (
        min(local_rs), max(local_rs), min(local_us), max(local_us),
        origin, right, up,
    )


def _from_local(origin, right, up, r, u):
    return origin + right * r + up * u


def _rect_corners(origin, right, up, r0, r1, u0, u1):
    return [
        _from_local(origin, right, up, r0, u0),
        _from_local(origin, right, up, r1, u0),
        _from_local(origin, right, up, r1, u1),
        _from_local(origin, right, up, r0, u1),
    ]


def _build_curve_loop(corners):
    curve_loop = CurveLoop()
    for i in range(4):
        curve_loop.Append(Line.CreateBound(corners[i], corners[(i + 1) % 4]))
    return curve_loop


def _expand_bounds_uniform(raw_min_r, raw_max_r, raw_min_u, raw_max_u, offset_ft):
    """Каркас щита + одинаковый выпуск со всех сторон."""
    return (
        raw_min_r - offset_ft,
        raw_max_r + offset_ft,
        raw_min_u - offset_ft,
        raw_max_u + offset_ft,
    )


def _outline_from_local_bounds(origin, right, up, min_r, max_r, min_u, max_u):
    corners = _rect_corners(origin, right, up, min_r, max_r, min_u, max_u)
    xs = [c.X for c in corners]
    ys = [c.Y for c in corners]
    zs = [c.Z for c in corners]
    return Outline(XYZ(min(xs), min(ys), min(zs)), XYZ(max(xs), max(ys), max(zs)))


def _pick_shield_rectangle(prompt1, prompt2):
    """
    Запрашивает у пользователя угол щита.
    Возвращает (context, error_text); context — словарь с view и границами.
    """
    view = doc.ActiveView
    _ensure_work_plane(doc, view)

    p1 = uidoc.Selection.PickPoint(prompt1)
    p2 = uidoc.Selection.PickPoint(prompt2)
    if p1 is None or p2 is None:
        return None, u"Не удалось получить точки"
    if p1.DistanceTo(p2) < _mm_to_feet(10):
        return None, u"Выбранные точки находятся слишком близко друг к другу"

    raw = _get_local_bounds(p1, p2, view)
    return {
        "view": view,
        "raw_min_r": raw[0], "raw_max_r": raw[1],
        "raw_min_u": raw[2], "raw_max_u": raw[3],
        "origin": raw[4], "right": raw[5], "up": raw[6],
    }, None


def _apply_expanded_bounds(ctx, offset_ft):
    """Добавляет в context границы контура с выпуском."""
    ctx["min_r"], ctx["max_r"], ctx["min_u"], ctx["max_u"] = _expand_bounds_uniform(
        ctx["raw_min_r"], ctx["raw_max_r"],
        ctx["raw_min_u"], ctx["raw_max_u"],
        offset_ft,
    )
    return ctx


# --- Марка щита ---

def _get_shield_mark_from_view(doc, view):
    try:
        assembly_id = view.AssociatedAssemblyInstanceId
    except Exception:
        return None
    if assembly_id is None or assembly_id == ElementId.InvalidElementId:
        return None

    assembly = doc.GetElement(assembly_id)
    if assembly is None:
        return None

    mark = _read_mark_parameter(assembly)
    if mark:
        return mark

    try:
        member_ids = assembly.GetMemberIds()
    except Exception:
        member_ids = []

    for member_id in member_ids:
        member = doc.GetElement(member_id)
        if member is None or member.Category is None:
            continue
        if member.Category.Id == ElementId(BuiltInCategory.OST_Walls):
            mark = _read_mark_parameter(member)
            if mark:
                return mark
    return None


def _get_shield_mark_from_bounds(doc, view, origin, right, up, min_r, max_r, min_u, max_u):
    try:
        bb_filter = BoundingBoxIntersectsFilter(
            _outline_from_local_bounds(origin, right, up, min_r, max_r, min_u, max_u)
        )
    except Exception:
        return None

    collector = (
        FilteredElementCollector(doc, view.Id)
        .OfCategory(BuiltInCategory.OST_Walls)
        .WherePasses(bb_filter)
    )
    try:
        for wall in collector:
            mark = _read_mark_parameter(wall)
            if mark:
                return mark
    finally:
        collector.Dispose()
    return None


def _get_shield_mark(doc, view, origin, right, up, min_r, max_r, min_u, max_u):
    return (
        _get_shield_mark_from_view(doc, view)
        or _get_shield_mark_from_bounds(
            doc, view, origin, right, up, min_r, max_r, min_u, max_u
        )
    )


# --- Раскладка полотен ---

def _calculate_sheet_count(total_h_mm, roll_w_mm, min_overlap_mm):
    """Минимальное число полотен, покрывающее габарит при мин. нахлесте."""
    if total_h_mm <= 0:
        return 0
    if total_h_mm <= roll_w_mm:
        return 1

    pitch = roll_w_mm - min_overlap_mm
    if pitch <= 0:
        raise ValueError(
            u"Минимальный нахлест не может быть больше или равен ширине рулона"
        )

    # Первое полотно дает полную ширину рулона, каждое следующее добавляет
    # только шаг (ширина рулона минус нахлест). Берем минимальный N,
    # при котором суммарное покрытие не меньше требуемого габарита.
    sheet_count = int(math.ceil((total_h_mm - roll_w_mm) / float(pitch))) + 1
    sheet_count = max(sheet_count, 2)

    if sheet_count > MAX_SHEET_COUNT:
        raise ValueError(
            u"Слишком большое количество полотен ({}). "
            u"Проверьте выпуск и габариты щита.".format(sheet_count)
        )
    return sheet_count


def _calculate_whole_edge_layout(total_h_mm, roll_w_mm, min_overlap_mm):
    """
    Раскладка «целые края + добор по центру»:
    нижнее и верхнее полотно целые, средние — пачка по центру с шагом (W − нахлест).
    """
    sheet_count = _calculate_sheet_count(total_h_mm, roll_w_mm, min_overlap_mm)
    if sheet_count == 0:
        return []

    if sheet_count == 1:
        # Одно полотно = контур с выпуском (не ширина рулона), чтобы выпуски были симметричны
        return [(0.0, total_h_mm)]

    bottom_sheet = (0.0, roll_w_mm)
    top_sheet = (total_h_mm - roll_w_mm, total_h_mm)
    if sheet_count == 2:
        return [bottom_sheet, top_sheet]

    step = roll_w_mm - min_overlap_mm
    middle_count = sheet_count - 2
    group_start = (total_h_mm - (roll_w_mm + (middle_count - 1) * step)) / 2.0

    positions = [bottom_sheet]
    for index in range(middle_count):
        start_mm = group_start + index * step
        positions.append((start_mm, start_mm + roll_w_mm))
    positions.append(top_sheet)
    return positions


def _layout_to_strip_bounds(positions_mm, min_r, max_r, min_u, max_u, horizontal):
    """Позиции вдоль раскладки → прямоугольники (r0,r1,u0,u1) для FilledRegion."""
    span_origin = min_u if horizontal else min_r
    strips = []
    for start_mm, end_mm in positions_mm:
        start_ft = _mm_to_feet(start_mm)
        end_ft = _mm_to_feet(end_mm)
        if horizontal:
            strips.append((min_r, max_r, span_origin + start_ft, span_origin + end_ft))
        else:
            strips.append((span_origin + start_ft, span_origin + end_ft, min_u, max_u))
    return strips


# --- Записи полотен (для размеров и марок) ---

def _make_region_record(region, origin, right, up, horizontal, r0, r1, u0, u1):
    span_start, span_end = (u0, u1) if horizontal else (r0, r1)
    return {
        "region": region,
        "span_start": span_start,
        "span_end": span_end,
        "bounds": (r0, r1, u0, u1),
        "edges": _region_edge_points(origin, right, up, horizontal, r0, r1, u0, u1),
    }


def _region_edge_points(origin, right, up, horizontal, r0, r1, u0, u1):
    c00 = _from_local(origin, right, up, r0, u0)
    c10 = _from_local(origin, right, up, r1, u0)
    c11 = _from_local(origin, right, up, r1, u1)
    c01 = _from_local(origin, right, up, r0, u1)
    if horizontal:
        return {
            "span_start": (c00, c10), "span_end": (c01, c11),
            "cross_min": (c00, c01), "cross_max": (c10, c11),
        }
    return {
        "span_start": (c00, c01), "span_end": (c10, c11),
        "cross_min": (c00, c10), "cross_max": (c01, c11),
    }


# --- Марки (теги) ---

def _create_region_tags(doc, view, region_records, origin, right, up):
    """«Маркировать по категории» — тег в центре каждой области."""
    errors = []
    for rec in region_records:
        r0, r1, u0, u1 = rec["bounds"]
        center = _from_local(origin, right, up, (r0 + r1) / 2.0, (u0 + u1) / 2.0)
        try:
            tag = IndependentTag.Create(
                doc, view.Id, Reference(rec["region"]),
                False, TagMode.TM_ADDBY_CATEGORY,
                TagOrientation.Horizontal, center,
            )
            if tag is None:
                errors.append(u"не удалось создать марку")
                continue
            try:
                tag.TagHeadPosition = center
            except Exception:
                pass
        except Exception as ex:
            errors.append(str(ex))

    if errors:
        raise RuntimeError(u"; ".join(errors[:3]))


# --- Размерные линии ---

def _get_region_edge_reference(view, region, p_start, p_end, tolerance_ft=0.005):
    options = Options()
    options.ComputeReferences = True
    options.View = view
    try:
        geom = region.get_Geometry(options)
    except Exception:
        return None
    if geom is None:
        return None
    for obj in geom:
        try:
            e0, e1 = obj.GetEndPoint(0), obj.GetEndPoint(1)
        except Exception:
            continue
        if ((e0.DistanceTo(p_start) <= tolerance_ft and e1.DistanceTo(p_end) <= tolerance_ft)
                or (e0.DistanceTo(p_end) <= tolerance_ft and e1.DistanceTo(p_start) <= tolerance_ft)):
            try:
                return obj.Reference
            except Exception:
                return None
    return None


def _create_detail_line(doc, view, p_start, p_end):
    return doc.Create.NewDetailCurve(view, Line.CreateBound(p_start, p_end))


def _create_tick_line(doc, view, center_point, tick_vec, tick_len_ft):
    half = tick_len_ft / 2.0
    return _create_detail_line(
        doc, view,
        center_point - tick_vec * half,
        center_point + tick_vec * half,
    )


def _new_dimension(doc, view, dim_line, references, dim_type):
    ref_array = ReferenceArray()
    for ref in references:
        ref_array.Append(ref)
    if dim_type is not None:
        return doc.Create.NewDimension(view, dim_line, ref_array, dim_type)
    return doc.Create.NewDimension(view, dim_line, ref_array)


def _create_membrane_dimensions(
    doc, view, origin, right, up, horizontal, region_records,
    raw_min_r, raw_max_r, raw_min_u, raw_max_u,
    min_r, max_r, min_u, max_u, dim_type,
):
    """
    Группы размеров:
      1 — размер каждого полотна (с «ступенькой», чтобы не наезжали);
      2 — общая ширина/длина по поперечной оси;
      3 — выпуск относительно щита: [выпуск]-[щит]-[выпуск];
      4а — нахлесты между полотнами;
      4б — выпуск относительно каркаса (точки клика пользователя).
    """
    # span — направление раскладки; cross — поперёк щита
    span_vec = up if horizontal else right
    cross_vec = right if horizontal else up

    span_min = min_u if horizontal else min_r
    span_max = max_u if horizontal else max_r
    cross_min = min_r if horizontal else min_u
    cross_max = max_r if horizontal else max_u

    raw_span_min = raw_min_u if horizontal else raw_min_r
    raw_span_max = raw_max_u if horizontal else raw_max_r
    raw_cross_min = raw_min_r if horizontal else raw_min_u
    raw_cross_max = raw_max_r if horizontal else raw_max_u

    def pt(cross_val, span_val):
        return origin + cross_vec * cross_val + span_vec * span_val

    try:
        view_scale = view.Scale if view.Scale and view.Scale > 0 else 1
    except Exception:
        view_scale = 1

    tick_len_ft = _mm_to_feet(DIM_TICK_LEN_PAPER_MM * view_scale)
    witness1_ft = _mm_to_feet(DIM_WITNESS_1_PAPER_MM * view_scale)
    witness2_ft = _mm_to_feet(DIM_WITNESS_2_PAPER_MM * view_scale)
    stagger_ft = _mm_to_feet(DIM_STAGGER_PAPER_MM * view_scale)

    doc.Regenerate()

    sorted_records = sorted(region_records, key=lambda r: r["span_start"])
    bottom_record = sorted_records[0]
    top_record = sorted_records[-1]
    stagger_index = {id(rec): i for i, rec in enumerate(sorted_records)}

    def _edge_ref(rec, key):
        p_a, p_b = rec["edges"][key]
        return _get_region_edge_reference(view, rec["region"], p_a, p_b)

    new_ticks = []

    def _fallback_tick(cross_val, span_val, tick_vec):
        tick_line = _create_tick_line(doc, view, pt(cross_val, span_val), tick_vec, tick_len_ft)
        new_ticks.append(tick_line)
        return tick_line

    def _resolve(ref, cross_val, span_val, tick_vec):
        if ref is not None:
            return ("ref", ref)
        return ("tick", _fallback_tick(cross_val, span_val, tick_vec))

    plans = []

    # 1) Размер каждого полотна вдоль раскладки
    for rec in region_records:
        idx = stagger_index[id(rec)]
        dim1_cross = cross_min - witness1_ft - idx * stagger_ft
        resolved = [
            _resolve(_edge_ref(rec, "span_start"), cross_min, rec["span_start"], cross_vec),
            _resolve(_edge_ref(rec, "span_end"), cross_min, rec["span_end"], cross_vec),
        ]
        plans.append((
            Line.CreateBound(pt(dim1_cross, rec["span_start"]), pt(dim1_cross, rec["span_end"])),
            resolved,
        ))

    # 2) Общая поперечная длина
    resolved2 = [
        _resolve(_edge_ref(top_record, "cross_min"), cross_min, span_max, span_vec),
        _resolve(_edge_ref(top_record, "cross_max"), cross_max, span_max, span_vec),
    ]
    dim2_span = span_max + witness1_ft
    plans.append((
        Line.CreateBound(pt(cross_min, dim2_span), pt(cross_max, dim2_span)),
        resolved2,
    ))

    # 3) Выпуск относительно щита (засечки на каркасе — граней в модели нет)
    resolved3 = [
        _resolve(_edge_ref(bottom_record, "cross_min"), cross_min, span_min, span_vec),
        ("tick", _fallback_tick(raw_cross_min, span_min, span_vec)),
        ("tick", _fallback_tick(raw_cross_max, span_min, span_vec)),
        _resolve(_edge_ref(bottom_record, "cross_max"), cross_max, span_min, span_vec),
    ]
    plans.append((
        Line.CreateBound(pt(cross_min, span_min - witness1_ft), pt(cross_max, span_min - witness1_ft)),
        resolved3,
    ))

    # 4а) Нахлесты между соседними полотнами
    dim4a_cross = cross_max + witness1_ft
    for i in range(len(sorted_records) - 1):
        rec_a, rec_b = sorted_records[i], sorted_records[i + 1]
        overlap_start, overlap_end = rec_b["span_start"], rec_a["span_end"]
        if overlap_end - overlap_start <= _mm_to_feet(1):
            continue
        resolved4a = [
            _resolve(_edge_ref(rec_b, "span_start"), cross_max, overlap_start, cross_vec),
            _resolve(_edge_ref(rec_a, "span_end"), cross_max, overlap_end, cross_vec),
        ]
        plans.append((
            Line.CreateBound(pt(dim4a_cross, overlap_start), pt(dim4a_cross, overlap_end)),
            resolved4a,
        ))

    # 4б) Выпуск относительно каркаса (исходные точки клика)
    resolved4b = [
        _resolve(_edge_ref(bottom_record, "span_start"), cross_max, span_min, cross_vec),
        ("tick", _fallback_tick(cross_max, raw_span_min, cross_vec)),
        ("tick", _fallback_tick(cross_max, raw_span_max, cross_vec)),
        _resolve(_edge_ref(top_record, "span_end"), cross_max, span_max, cross_vec),
    ]
    dim4b_cross = cross_max + witness2_ft
    plans.append((
        Line.CreateBound(pt(dim4b_cross, span_min), pt(dim4b_cross, span_max)),
        resolved4b,
    ))

    if new_ticks:
        doc.Regenerate()

    errors = []
    for dim_line, resolved in plans:
        references = []
        for kind, value in resolved:
            references.append(value if kind == "ref" else value.GeometryCurve.Reference)
        try:
            _new_dimension(doc, view, dim_line, references, dim_type)
        except Exception as ex:
            errors.append(str(ex))

    if errors:
        raise RuntimeError(u"; ".join(errors[:3]))


# --- Окно инструмента ---

class FramingLayersWindow(forms.WPFWindow):
    def __init__(self):
        xaml_path = os.path.join(os.path.dirname(__file__), "ui_template.xaml")
        forms.WPFWindow.__init__(self, xaml_path)
        self.btn_run.Click += self.btn_run_click
        self.cb_regions.SelectionChanged += self.cb_regions_selection_changed
        self.Closing += self._on_closing

        saved_left, saved_top = _load_window_position()
        if saved_left is not None and saved_top is not None:
            saved_left, saved_top = _clamp_to_screen(saved_left, saved_top)
            self.WindowStartupLocation = WindowStartupLocation.Manual
            self.Left = saved_left
            self.Top = saved_top

        self._roll_width_mm = 1500
        self._region_types = {}
        collector = FilteredElementCollector(doc).OfClass(FilledRegionType)
        try:
            for element_type in collector.ToElements():
                self._region_types[_get_type_name(element_type)] = element_type
        finally:
            collector.Dispose()

        type_names = sorted(self._region_types.keys())
        self.cb_regions.ItemsSource = type_names
        if type_names:
            self.cb_regions.SelectedIndex = 0
        else:
            self.cb_regions_selection_changed(None, None)

    def cb_regions_selection_changed(self, sender, args):
        selected_name = self.cb_regions.SelectedItem
        self._roll_width_mm = _resolve_roll_width_mm(selected_name)
        self.lbl_roll_width.Text = u"Ширина рулона: {} мм".format(self._roll_width_mm)

    def _on_closing(self, sender, args):
        try:
            _save_window_position(self.Left, self.Top)
        except Exception:
            pass

    def _read_form_params(self):
        """Проверяет поля окна; при ошибке показывает alert и возвращает None."""
        selected_name = self.cb_regions.SelectedItem
        region_type = self._region_types.get(selected_name)
        if not region_type:
            forms.alert(u"Выберите тип пленки", title=u"Ошибка")
            return None

        offset_mm = _parse_offset_mm(self.tb_offset.Text)
        if offset_mm is None:
            forms.alert(
                u"Некорректное значение выпуска. Допустимый диапазон: "
                u"от 0 до {} мм.".format(int(MAX_OFFSET_MM)),
                title=u"Ошибка",
            )
            return None

        direction = _get_combo_text(self.cb_direction)
        if direction not in (u"Горизонтально", u"Вертикально"):
            forms.alert(u"Выберите направление раскладки", title=u"Ошибка")
            return None

        return {
            "selected_name": selected_name,
            "region_type": region_type,
            "horizontal": direction == u"Горизонтально",
            "offset_ft": _mm_to_feet(offset_mm),
            "roll_w_mm": self._roll_width_mm,
        }

    def _run_dimensions(self, ctx, horizontal, region_records, dim_type):
        _create_membrane_dimensions(
            doc, ctx["view"], ctx["origin"], ctx["right"], ctx["up"],
            horizontal, region_records,
            ctx["raw_min_r"], ctx["raw_max_r"], ctx["raw_min_u"], ctx["raw_max_u"],
            ctx["min_r"], ctx["max_r"], ctx["min_u"], ctx["max_u"],
            dim_type,
        )

    def btn_run_click(self, sender, args):
        params = self._read_form_params()
        if params is None:
            return

        self.Hide()
        try:
            ctx, err = _pick_shield_rectangle(
                u"Укажите первую точку угла щита",
                u"Укажите противоположную точку по диагонали",
            )
            if err:
                forms.alert(err, title=u"Ошибка")
                self.Show()
                return

            _apply_expanded_bounds(ctx, params["offset_ft"])
            horizontal = params["horizontal"]

            span_ft = (ctx["max_u"] - ctx["min_u"]) if horizontal else (ctx["max_r"] - ctx["min_r"])
            total_h_mm = _feet_to_mm(span_ft)
            if not _is_finite_positive(total_h_mm):
                forms.alert(u"Некорректные габариты щита. Повторите выбор точек.", title=u"Ошибка")
                self.Show()
                return

            positions_mm = _calculate_whole_edge_layout(total_h_mm, params["roll_w_mm"], MIN_OVERLAP_MM)
            if not positions_mm:
                forms.alert(u"Не удалось построить полосы раскладки", title=u"Ошибка")
                self.Show()
                return

            strip_bounds = _layout_to_strip_bounds(
                positions_mm,
                ctx["min_r"], ctx["max_r"], ctx["min_u"], ctx["max_u"],
                horizontal,
            )

            mark_prefix = (
                _get_shield_mark(
                    doc, ctx["view"], ctx["origin"], ctx["right"], ctx["up"],
                    ctx["raw_min_r"], ctx["raw_max_r"], ctx["raw_min_u"], ctx["raw_max_u"],
                )
                or _mark_prefix_from_type(params["selected_name"])
            )
            dim_type = _find_element_type(doc, DimensionType, DIMENSION_TYPE_NAME)

            transaction = Transaction(doc, u"Размещение пленки")
            transaction.Start()
            try:
                region_records = []
                for index, bounds in enumerate(strip_bounds, start=1):
                    r0, r1, u0, u1 = bounds
                    loop_list = List[CurveLoop]()
                    loop_list.Add(_build_curve_loop(
                        _rect_corners(ctx["origin"], ctx["right"], ctx["up"], r0, r1, u0, u1)
                    ))
                    region = FilledRegion.Create(
                        doc, params["region_type"].Id, ctx["view"].Id, loop_list,
                    )
                    _set_mark_parameter(region, u"{}-{}".format(mark_prefix, index))
                    region_records.append(_make_region_record(
                        region, ctx["origin"], ctx["right"], ctx["up"],
                        horizontal, r0, r1, u0, u1,
                    ))

                doc.Regenerate()

                try:
                    _create_region_tags(
                        doc, ctx["view"], region_records,
                        ctx["origin"], ctx["right"], ctx["up"],
                    )
                except Exception as tag_ex:
                    forms.alert(
                        u"Пленка размещена, но не удалось создать марки:\n{}".format(tag_ex),
                        title=u"Предупреждение",
                    )

                try:
                    self._run_dimensions(ctx, horizontal, region_records, dim_type)
                except Exception as dim_ex:
                    forms.alert(
                        u"Пленка размещена, но не удалось создать часть "
                        u"размерных линий:\n{}".format(dim_ex),
                        title=u"Предупреждение",
                    )

                transaction.Commit()
            except Exception:
                transaction.RollBack()
                raise

            self.Close()
        except OperationCanceledException:
            forms.alert(u"Выбор точек отменен")
            self.Close()
        except Exception as ex:
            forms.alert(str(ex), title=u"Ошибка")
            self.Show()


try:
    window = FramingLayersWindow()
    window.ShowDialog()
except Exception as ex:
    forms.alert(str(ex), title=u"Ошибка")
