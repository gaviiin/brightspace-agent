import { createBrowserRouter } from "react-router-dom";

import { CourseListPage } from "./pages/CourseListPage";
import { CourseWorkspacePage } from "./pages/CourseWorkspacePage";
import { SettingsPage } from "./pages/SettingsPage";

export const router = createBrowserRouter([
  { path: "/", element: <CourseListPage /> },
  { path: "/courses/:courseId", element: <CourseWorkspacePage /> },
  { path: "/settings", element: <SettingsPage /> },
]);
