# -*- coding: utf-8 -*-
"""Поиск элементов по значению параметра и подсветка в модели."""
import os

import clr

clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")

from System.Collections.Generic import List
from Autodesk.Revit.DB import ElementId, FilteredElementCollector, StorageType
from pyrevit import forms, revit


doc = revit.doc
uidoc = revit.uidoc


def _add_param_names_from_element(element, names):
    if element is None:
        return
    try:
        for param in element.Parameters:
            if param is None or param.Definition is None:
                continue
            name = param.Definition.Name
            if name:
                names.add(name)
    except Exception:
        pass


def _collect_project_parameter_names(document):
    """Все имена параметров, встречающиеся в проекте."""
    names = set()

    binding_iter = document.ParameterBindings.ForwardIterator()
    binding_iter.Reset()
    while binding_iter.MoveNext():
        definition = binding_iter.Key
        if definition is None:
            continue
        try:
            name = definition.Name
            if name:
                names.add(name)
        except Exception:
            pass

    for element in FilteredElementCollector(document).WhereElementIsNotElementType():
        _add_param_names_from_element(element, names)

    for element in FilteredElementCollector(document).WhereElementIsElementType():
        _add_param_names_from_element(element, names)

    return sorted(names, key=lambda item: item.lower())


def _param_display_value(document, param):
    if param is None or not param.HasValue:
        return None
    try:
        storage = param.StorageType
        if storage == StorageType.String:
            return param.AsString()
        if storage == StorageType.Integer:
            return str(param.AsInteger())
        if storage == StorageType.Double:
            value_string = param.AsValueString()
            if value_string:
                return value_string
            return str(param.AsDouble())
        if storage == StorageType.ElementId:
            elem_id = param.AsElementId()
            if elem_id is None or elem_id == ElementId.InvalidElementId:
                return None
            linked = document.GetElement(elem_id)
            if linked is not None:
                try:
                    return linked.Name
                except Exception:
                    pass
            try:
                return str(elem_id.Value)
            except Exception:
                return str(elem_id.IntegerValue)
    except Exception:
        pass
    return None


def _normalize_text(value):
    if value is None:
        return u""
    return unicode(value).strip().lower()


def _values_match(param, search_text, document):
    display = _param_display_value(document, param)
    search_norm = _normalize_text(search_text)
    if not search_norm:
        return False

    display_norm = _normalize_text(display)
    if not display_norm:
        return False

    return search_norm in display_norm


def _element_has_matching_param(document, element, param_name, search_text):
    candidates = [element]
    try:
        type_elem = document.GetElement(element.GetTypeId())
    except Exception:
        type_elem = None
    if type_elem is not None:
        candidates.append(type_elem)

    for candidate in candidates:
        try:
            param = candidate.LookupParameter(param_name)
        except Exception:
            param = None
        if param is None:
            continue
        if _values_match(param, search_text, document):
            return True
    return False


def _find_matching_element_ids(document, param_name, search_text):
    matched_ids = []
    for element in FilteredElementCollector(document).WhereElementIsNotElementType():
        try:
            if _element_has_matching_param(document, element, param_name, search_text):
                matched_ids.append(element.Id)
        except Exception:
            continue
    return matched_ids


def _highlight_elements(element_ids):
    id_list = List[ElementId]()
    for elem_id in element_ids:
        id_list.Add(elem_id)
    uidoc.Selection.SetElementIds(id_list)
    uidoc.ShowElements(id_list)


def _get_combo_text(combo):
    text = combo.Text
    if text:
        return text.strip()
    item = combo.SelectedItem
    if item is None:
        return u""
    return unicode(item).strip()


class SearchWindow(forms.WPFWindow):
    def __init__(self, param_names):
        xaml_path = os.path.join(os.path.dirname(__file__), "ui_template.xaml")
        forms.WPFWindow.__init__(self, xaml_path)
        self.btn_search.Click += self.btn_search_click
        self.cb_param.ItemsSource = param_names
        if param_names:
            self.cb_param.SelectedIndex = 0

    def btn_search_click(self, sender, args):
        param_name = _get_combo_text(self.cb_param)
        search_text = self.tb_value.Text.strip()

        if not param_name:
            forms.alert(u"Выберите параметр", title=u"search")
            return
        if not search_text:
            forms.alert(u"Введите значение для поиска", title=u"search")
            return

        matched_ids = _find_matching_element_ids(doc, param_name, search_text)
        if not matched_ids:
            forms.alert(
                u'Элементы с параметром "{}" = "{}" не найдены.'.format(
                    param_name, search_text
                ),
                title=u"search",
            )
            return

        _highlight_elements(matched_ids)


try:
    param_names = _collect_project_parameter_names(doc)
    if not param_names:
        forms.alert(u"В проекте не найдено параметров.", title=u"search")
    else:
        SearchWindow(param_names).ShowDialog()
except Exception as ex:
    forms.alert(str(ex), title=u"search")
